"""Exercise the image workflows through the workbench API."""
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
import pytest
import requests

from video_agent_studio.app import create_app
from video_agent_studio.adapters.base import artifact
from video_agent_studio.config import Settings

from video_agent_studio.tests.test_http_projects import response
from video_agent_studio.tests.test_integration import execute, plan, session, upload, wait_job


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail('Unexpected backend request')

    monkeypatch.setattr(requests.Session, 'request', unexpected_request)
    settings = Settings(data_dir=tmp_path, planner_mode='rules', poll_interval=0.01)
    app = create_app(settings, check_tools=False)
    with TestClient(app) as client:
        client.headers['X-App-Token'] = client.get('/api/settings').json()['app_token']
        yield client, app, settings, monkeypatch


def png_response():
    buffer = BytesIO()
    Image.new('RGB', (8, 12), 'white').save(buffer, format='PNG')
    return response(chunks=[buffer.getvalue()])


def test_reuse_selected_result_for_look_and_talking_even_if_batch_order_changes(workbench):
    client, app, settings, _ = workbench
    identity = session(client)
    portrait = plan(client, identity, '生成两张坐姿人像。', tool='portrait_image')
    store = app.state.store
    job = store.create_job(portrait, 'reuse-reference-test')
    folder = settings.data_dir / 'jobs' / job['id']
    folder.mkdir(parents=True)
    first, second = folder / 'first.png', folder / 'second.png'
    Image.new('RGB', (8, 12), 'red').save(first)
    Image.new('RGB', (8, 12), 'blue').save(second)
    first_image, second_image = artifact(first, '人像 1', 'image'), artifact(second, '人像 2', 'image')
    job_url = f'/api/jobs/{job["id"]}'
    store.update('jobs', job['id'], state='running', artifacts=[second_image])
    early = client.get(job_url).json()['artifacts'][0]
    store.update('jobs', job['id'], state='completed', artifacts=[first_image, second_image])
    images = client.get(job_url).json()['artifacts']
    assert early['url'] == images[1]['url'] and early['reference_url'] == images[1]['reference_url']

    selected = client.post(early['reference_url'])
    assert selected.status_code == 200 and 'path' not in selected.json()
    selected = selected.json()
    with Image.open(BytesIO(client.get(selected['url']).content)) as image:
        assert image.getpixel((0, 0)) == (0, 0, 255)
    look = plan(client, identity, '用 Qwen 换发型。', tool='qwen_image')
    assert look['ready'] and look['asset_ids']['reference'] == selected['id']

    other = client.post(images[0]['reference_url']).json()
    detail = client.get(f'/api/sessions/{identity}').json()
    assert [asset['id'] for asset in detail['assets']] == [other['id'], selected['id']]
    assert client.post(early['reference_url']).json()['id'] == selected['id']
    talking = plan(client, identity, '口播文案：大家好！', tool='talking')
    assert talking['ready'] and talking['asset_ids']['reference'] == selected['id']
    detail = client.get(f'/api/sessions/{identity}').json()
    assert [asset['id'] for asset in detail['assets']] == [selected['id'], other['id']]


@pytest.mark.parametrize('partial', [False, True])
def test_qwen_submits_uploaded_slots_and_preserves_confirmed_images(workbench, partial):
    client, app, settings, monkeypatch = workbench
    identity = session(client)
    proposal = plan(client, identity, '用 Qwen 给参考人物换装，生成两张 3:4 图片。')
    assert proposal['tool'] == 'qwen_image' and not proposal['ready']
    for role in ('reference', 'costume', 'pose', 'hair'):
        assert upload(client, identity, role).status_code == 200
    proposal = plan(client, identity, '继续')
    assert proposal['ready'] and proposal['options']['count'] == 2

    uploaded = []
    submitted = []
    backend_jobs = [str(uuid4()) for _ in range(1 if partial else 2)]

    def backend(self, method, url, **kwargs):
        assert url.startswith(settings.qwen_image_url)
        if method == 'POST' and url.endswith('/api/uploads'):
            assert kwargs['data'].startswith(b'\xff\xd8')
            uploaded.append(str(uuid4()))
            return response({'upload': uploaded[-1]}, 201)
        if method == 'POST' and url.endswith('/api/jobs'):
            submitted.append(kwargs['json'])
            return response({'jobs': [{'id': job} for job in backend_jobs]}, 202)
        if method == 'GET' and url.endswith('/image'):
            assert url.split('/')[-2] in backend_jobs
            return png_response()
        assert method == 'GET' and url.split('/')[-1] in backend_jobs
        return response({'state': 'success', 'image': 'https://unexpected.example/image'})

    monkeypatch.setattr(requests.Session, 'request', backend)
    job = execute(client, proposal['id']).json()
    saved = wait_job(client, app, job['id'], ('failed' if partial else 'completed',))
    assert len(submitted) == 1
    assert submitted[0]['references'] == dict(zip(('portrait', 'costume', 'pose', 'hair'),
                                                 ({'upload': item} for item in uploaded)))
    assert submitted[0]['ratio'] == '3:4'
    assert len(saved['artifacts']) == len(backend_jobs)
    attachment = client.get(saved['artifacts'][0]['url'] + '?download=true')
    assert attachment.headers['content-type'] == 'image/png'
    assert attachment.headers['content-disposition'].startswith('attachment')
    if partial:
        assert client.post(f'/api/jobs/{job["id"]}/resume', json={}).status_code == 200
        wait_job(client, app, job['id'], ('failed',))
        assert len(submitted) == 1


def test_portrait_lost_response_retries_original_uuid_then_downloads_batch(workbench):
    client, app, settings, monkeypatch = workbench
    identity = session(client)
    proposal = plan(client, identity, '生成两张白底的欧美通勤坐姿人像，画幅9:16。')
    assert proposal['tool'] == 'portrait_image' and proposal['ready'] and proposal['asset_ids'] == {}
    submitted = []
    child = uuid4().hex

    def backend(self, method, url, **kwargs):
        assert url.startswith(settings.portrait_image_url)
        if url.endswith('/api/settings'):
            return response({'token': 'test-token'})
        if method == 'POST' and url.endswith('/api/jobs'):
            assert kwargs['headers'] == {'X-App-Token': 'test-token'}
            submitted.append(kwargs['json'])
            if len(submitted) == 1:
                raise requests.Timeout()
            return response({'batch_ids': [submitted[0]['request_id'], child]})
        if '/images/' in url:
            assert url.endswith('/images/image-1.png')
            return png_response()
        assert method == 'GET' and url.split('/')[-1] in (submitted[0]['request_id'], child)
        return response({'status': 'completed', 'images': ['image-1.png']})

    monkeypatch.setattr(requests.Session, 'request', backend)
    job = execute(client, proposal['id']).json()
    wait_job(client, app, job['id'], ('failed',))
    assert client.post(f'/api/jobs/{job["id"]}/resume', json={}).status_code == 200
    saved = wait_job(client, app, job['id'])
    assert submitted[0] == submitted[1]
    assert {k: submitted[0][k] for k in ('request_id', 'count', 'background', 'region', 'style', 'aspect')} == {
        'request_id': job['id'], 'count': 2, 'background': 'white', 'region': 'western', 'style': 'office', 'aspect': '9:16',
    }
    assert [a['label'] for a in saved['artifacts']] == ['人像 1', '人像 2']


def test_qwen_ambiguous_submission_requires_existing_job_binding(workbench):
    client, app, settings, monkeypatch = workbench
    identity = session(client)
    assert upload(client, identity, 'reference').status_code == 200
    proposal = plan(client, identity, '用 H3 Image 改发型，出一张。')
    assert proposal['options']['model'] == 'h3-image'
    remote = str(uuid4())
    submitted = []

    def backend(self, method, url, **kwargs):
        assert url.startswith(settings.qwen_image_url)
        if url.endswith('/api/uploads'):
            return response({'upload': str(uuid4())}, 201)
        if url.endswith('/api/jobs'):
            submitted.append(kwargs['json'])
            raise requests.Timeout()
        if url.endswith(f'/{remote}/image'):
            return png_response()
        assert url.endswith(f'/{remote}')
        return response({'state': 'success'})

    monkeypatch.setattr(requests.Session, 'request', backend)
    job = execute(client, proposal['id']).json()
    saved = wait_job(client, app, job['id'], ('submission_unknown',))
    assert saved['backend_url'] == settings.qwen_image_url + '/'
    endpoint = f'/api/jobs/{job["id"]}/resume'
    assert client.post(endpoint, json={}).status_code == 409
    assert client.post(endpoint, json={'remote_job_id': 'not-a-uuid'}).status_code == 409
    assert client.post(endpoint, json={'remote_job_id': remote}).status_code == 200
    wait_job(client, app, job['id'])
    assert len(submitted) == 1


def background_png(color):
    buffer = BytesIO()
    Image.new('RGBA', (128, 192), color).save(buffer, 'PNG')
    return buffer.getvalue()


@pytest.mark.parametrize('outcome', ['done', 'partial', 'lost_receipt'])
def test_background_reuses_only_whole_scene_and_keeps_layers(workbench, outcome):
    client, app, settings, monkeypatch = workbench
    identity = session(client)
    proposal = plan(client, identity, '用两张商品图做素材布景。商品名：衬衫；标语：夏日轻装')
    assert proposal['tool'] == 'background' and not proposal['ready']
    assert upload(client, identity, 'scene').status_code == 400
    for color in ('red', 'blue'):
        assert upload(client, identity, 'product', color + '.png', background_png(color)).status_code == 200
    original_scene = upload(client, identity, 'scene', 'studio.png', background_png('white')).json()
    proposal = plan(client, identity, '继续')
    assert proposal['ready'] and proposal['asset_ids']['scene'] == original_scene['id']

    remote = '1126_120000_a12f'
    product_id, scene_id = 'a' * 10, 'b' * 16
    prefix = f'/out/jobs/{remote}'
    issued = []
    transparent = background_png((255, 0, 0, 0))
    whole = background_png('gray')

    def backend(self, method, url, **kwargs):
        assert url.startswith(settings.background_url)
        path = url.removeprefix(settings.background_url)
        if method == 'POST' and path == '/api/upload':
            assert kwargs['data'] == {'scene_mode': 'reference'}
            colors = []
            for field, (_, file, mime) in kwargs['files']:
                assert field == 'files' and mime == 'image/png'
                with Image.open(file) as image:
                    colors.append(image.getpixel((0, 0)))
            assert colors == [(255, 0, 0), (0, 0, 255)]
            return response({'id': product_id})
        if method == 'POST' and path == '/api/scene-reference':
            assert kwargs['files']['file'][2] == 'image/png'
            return response({'id': scene_id})
        if method == 'POST' and path == '/api/generate':
            issued.append(kwargs['json'])
            if outcome == 'lost_receipt':
                raise requests.Timeout()
            return response({'job_id': remote})
        if method == 'GET' and path == f'/api/job/{remote}':
            layers = [] if outcome == 'partial' else [{'fg': f'{prefix}/fg.png', 'bg': f'{prefix}/bg.png'}]
            return response({'id': remote, 'scene_mode': 'reference', 'status': 'partial' if outcome == 'partial' else 'done',
                             'results': [{'url': f'{prefix}/01_reference.png'}], 'layers': layers})
        assert method == 'GET' and path in {f'{prefix}/{name}.png' for name in ('fg', 'bg', '01_reference')}
        return response(chunks=[transparent if path.endswith('/fg.png') else whole])

    monkeypatch.setattr(requests.Session, 'request', backend)
    identity_job = execute(client, proposal['id']).json()['id']
    if outcome == 'lost_receipt':
        wait_job(client, app, identity_job, ('submission_unknown',))
        resume = f'/api/jobs/{identity_job}/resume'
        assert client.post(resume, json={}).status_code == 409
        assert client.post(resume, json={'remote_job_id': 'abc'}).status_code == 409
        assert client.post(resume, json={'remote_job_id': remote}).status_code == 200
    saved = wait_job(client, app, identity_job)
    assert issued == [{'id': product_id, 'scene_id': scene_id, 'scene_mode': 'reference',
                       'lang': 'zh', 'name': '衬衫', 'slogan': '夏日轻装'}]
    assert saved['remote_job_id'] == remote
    scene, *layers = saved['artifacts']
    assert scene['reuse_role'] == 'scene'
    selection = client.post(scene['reference_url']).json()
    assert selection['role'] == 'scene'
    assert client.post(scene['reference_url']).json()['id'] == selection['id']
    assert plan(client, identity, '使用新素材继续')['asset_ids']['scene'] == selection['id']
    assert not plan(client, identity, '口播文案：开播了！', tool='talking')['ready']

    if outcome == 'partial':
        assert not layers and saved['quality_status'] == 'needs_review'
        assert '缺少配套分层' in saved['details']['quality_note']
    else:
        assert [a['label'] for a in layers] == ['干净背景', '透明前景']
        assert all('reference_url' not in layer for layer in layers)
        with Image.open(BytesIO(client.get(layers[1]['url'] + '?download=true').content)) as image:
            assert image.getchannel('A').getextrema() == (0, 0)
        local_fg = app.state.store.get('jobs', identity_job)['artifacts'][-1]['path']
        key = sha256(local_fg.encode()).hexdigest()
        assert client.post(f'/api/jobs/{identity_job}/artifacts/{key}/reference').status_code == 400


@pytest.mark.parametrize(('mode', 'label'), [('booth', 'AI 电商展台'), ('scene', 'AI 实景房间')])
def test_background_automatic_modes_use_existing_product_and_fetch_matching_layers(workbench, mode, label):
    client, app, settings, monkeypatch = workbench
    identity = session(client)
    upload(client, identity)
    proposal = plan(client, identity, f'用商品图做{label}，暖调横屏、主播偏左，生成两张')
    assert proposal['ready'] and set(proposal['asset_ids']) == {'product'}
    remote = '1126_120001_a12f'
    background, foreground = f'/fg-split/{"a" * 16}_bg.png', f'/fg-split/{"a" * 16}_fg.png'

    def backend(self, method, url, **kwargs):
        assert url.startswith(settings.background_url)
        path = url.removeprefix(settings.background_url)
        if method == 'POST' and path == '/api/upload':
            assert kwargs['data'] == {'scene_mode': mode}
            return response({'id': 'a' * 10})
        if method == 'POST' and path == '/api/generate':
            assert kwargs['json'] == {'id': 'a' * 10, 'scene_mode': mode, 'lang': 'zh', 'count': 2,
                                      'aspect': 'landscape', 'styles': ['warm'], 'host_side': 'left', 'stick': False}
            return response({'job_id': remote})
        if path == f'/api/job/{remote}':
            return response({'id': remote, 'scene_mode': mode, 'status': 'done',
                             'results': [{'error': 'failed'}, {'url': f'/out/jobs/{remote}/02_warm.png'}]})
        if path == f'/api/assets/{remote}':
            return response({'layers': [{'name': 'unrelated.png', 'bg': '/ignore', 'fg': '/ignore'},
                                        {'name': '02_warm.png', 'bg': background + '?v=3', 'fg': foreground + '?v=3'}]})
        assert method == 'GET' and path in {f'/out/jobs/{remote}/02_warm.png', background, foreground}
        return png_response()

    monkeypatch.setattr(requests.Session, 'request', backend)
    job = wait_job(client, app, execute(client, proposal['id']).json()['id'])
    assert len(job['artifacts']) == 3 and '1/2' in job['details']['quality_note']
