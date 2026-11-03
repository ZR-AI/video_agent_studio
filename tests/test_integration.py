"""Exercise the real HTTP, LangGraph and SQLite boundaries without paid APIs."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from video_agent_studio.adapters.base import NeedsReview, SubmissionUnknown, artifact
from video_agent_studio.app import create_app
from video_agent_studio.config import Settings


class CompleteAdapter:
    def __init__(self):
        self.calls = []

    def execute(self, job, ctx):
        self.calls.append(job)
        ctx.check_cancelled()
        path = ctx.workdir / 'final.mp4'
        path.write_bytes(b'local-fake-video')
        return {'artifacts': [artifact(path)], 'details': job.get('details', {}),
                'quality_status': 'not_requested'}


@pytest.fixture
def studio(tmp_path, monkeypatch):
    def no_provider(*args, **kwargs):
        raise AssertionError('Integration tests must never contact a provider')
    monkeypatch.setattr('requests.sessions.Session.request', no_provider)
    monkeypatch.setattr('urllib.request.OpenerDirector.open', no_provider)
    settings = Settings(data_dir=tmp_path / 'data', planner_mode='rules', max_workers=1,
                        ds_key='never-send-ds-secret', rh_key='never-send-rh-secret')
    fake = CompleteAdapter()
    app = create_app(settings, adapters={name: fake for name in ('product', 'talking', 'motion')}, check_tools=False)
    with TestClient(app) as client:
        public = client.get('/api/settings').json()
        assert public['framework'] == 'LangGraph'
        client.headers['x-app-token'] = public['app_token']
        yield client, app, fake, settings


def session(client):
    response = client.post('/api/sessions')
    assert response.status_code == 200
    return response.json()['id']


def png():
    output = BytesIO()
    Image.new('RGBA', (4, 3), (255, 0, 0, 0)).save(output, 'PNG')
    return output.getvalue()


def upload(client, identity, role='product', filename='product.png', content=None):
    return client.post('/api/assets', data={'session_id': identity, 'role': role},
                       files={'file': (filename, png() if content is None else content, 'image/png')})


def plan(client, identity, text='生成10秒竖屏商品广告', tool='auto'):
    response = client.post(f'/api/sessions/{identity}/messages', json={'text': text, 'tool': tool})
    assert response.status_code == 200, response.text
    return response.json()['plan']


def execute(client, plan_id, request_id='request-test-001'):
    return client.post(f'/api/plans/{plan_id}/execute', json={'request_id': request_id})


def wait_job(client, app, identity, states=('completed',), timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f'/api/jobs/{identity}')
        assert response.status_code == 200
        job = response.json()
        with app.state.engine.lock:
            active = identity in app.state.engine.active
        if job['state'] in states and not active:
            return job
        time.sleep(0.01)
    pytest.fail(f'Job did not reach {states}: {job}')


def product_plan(client):
    identity = session(client)
    assert upload(client, identity).status_code == 200
    return identity, plan(client, identity)


def test_real_graph_stops_for_confirmation_then_finishes_and_downloads(studio):
    client, app, fake, settings = studio
    identity, proposal = product_plan(client)
    assert proposal['ready'] and proposal['tool'] == 'product'
    assert proposal['options']['seconds'] == 10 and proposal['asset_ids']['image']
    assert app.state.store.list('jobs') == [] and fake.calls == []
    snapshot = app.state.agent.graph.get_state({'configurable': {'thread_id': proposal['id']}})
    assert snapshot.next == ('review',)
    created = execute(client, proposal['id'])
    assert created.status_code == 200
    job = wait_job(client, app, created.json()['id'])
    assert len(fake.calls) == 1 and job['quality_status'] == 'not_requested'
    assert 'path' not in job['artifacts'][0]
    download = client.get(job['artifacts'][0]['url'] + '?download=true')
    assert download.content == b'local-fake-video' and 'attachment' in download.headers['content-disposition']
    latest = client.get(f'/api/sessions/{identity}').json()
    assert latest['jobs'][0]['id'] == job['id']
    assert 'never-send' not in str(latest)


def test_repeated_execute_is_idempotent_even_with_new_request_id(studio):
    client, app, fake, _ = studio
    _, proposal = product_plan(client)
    first = execute(client, proposal['id']).json()
    wait_job(client, app, first['id'])
    second = execute(client, proposal['id'], 'request-test-002')
    assert second.status_code == 200 and second.json()['id'] == first['id']
    assert len(fake.calls) == 1 and len(app.state.store.list('jobs')) == 1


def test_missing_material_never_reaches_adapter(studio):
    client, app, fake, _ = studio
    identity = session(client)
    proposal = plan(client, identity, '生成动作迁移视频')
    assert not proposal['ready'] and len(proposal['missing']) == 2
    assert execute(client, proposal['id']).status_code == 409
    assert fake.calls == [] and app.state.store.list('jobs') == []


def test_superseded_plan_rejected_and_followup_preserves_options(studio):
    client, app, fake, _ = studio
    identity, first = product_plan(client)
    second = plan(client, identity, '改成横屏')
    assert second['tool'] == 'product' and second['options']['aspect'] == '16:9'
    assert second['options']['seconds'] == 10
    assert execute(client, first['id']).status_code == 409
    assert fake.calls == []
    created = execute(client, second['id']).json()
    wait_job(client, app, created['id'])


def test_talking_optional_reference_review_preserves_preview(studio):
    client, app, _, _ = studio
    class ReviewAdapter(CompleteAdapter):
        def execute(self, job, ctx):
            if not job['details'].get('frames_approved'):
                path = ctx.workdir / 'holding.png'
                path.write_bytes(png())
                ctx.update(artifacts=[artifact(path, '持商品参考图', 'image')])
                raise NeedsReview('请确认参考帧')
            return super().execute(job, ctx)
    reviewed = ReviewAdapter()
    app.state.engine.adapters['talking'] = reviewed
    identity = session(client)
    assert upload(client, identity, 'reference').status_code == 200
    assert upload(client, identity, 'product').status_code == 200
    proposal = plan(client, identity, '人物口播，文案：这是完整口播文案。')
    assert proposal['tool'] == 'talking' and proposal['options']['script'] == '这是完整口播文案。'
    assert proposal['warnings']
    created = execute(client, proposal['id']).json()
    pending = wait_job(client, app, created['id'], ('awaiting_review',))
    assert client.get(pending['artifacts'][0]['url']).content == png()
    response = client.post(f'/api/jobs/{created["id"]}/resume', json={'frames_approved': True})
    assert response.status_code == 200
    final = wait_job(client, app, created['id'])
    assert final['details']['frames_approved'] and len(reviewed.calls) == 1


def test_pause_and_resume_reuses_persisted_cloud_identity(studio):
    client, app, _, _ = studio
    entered = threading.Event()
    class PausingAdapter(CompleteAdapter):
        def execute(self, job, ctx):
            if not job.get('remote_job_id'):
                ctx.update(remote_job_id='existing-cloud-job')
                entered.set()
                while True:
                    ctx.sleep(0.01)
            assert job['remote_job_id'] == 'existing-cloud-job'
            return super().execute(job, ctx)
    fake = PausingAdapter()
    app.state.engine.adapters['product'] = fake
    _, proposal = product_plan(client)
    created = execute(client, proposal['id']).json()
    assert entered.wait(timeout=3)
    assert client.post(f'/api/jobs/{created["id"]}/pause').status_code == 200
    paused = wait_job(client, app, created['id'], ('paused',))
    assert paused['remote_job_id'] == 'existing-cloud-job'
    assert client.post(f'/api/jobs/{created["id"]}/resume', json={}).status_code == 200
    completed = wait_job(client, app, created['id'])
    assert completed['remote_job_id'] == 'existing-cloud-job' and len(fake.calls) == 1


def test_talking_unknown_submission_requires_original_task_id(studio):
    client, app, _, _ = studio
    class UnknownAdapter(CompleteAdapter):
        def execute(self, job, ctx):
            rec = (job.get('details', {}).get('steps') or {}).get('segment:1', {})
            if not rec.get('task_id'):
                ctx.update(details={'steps': {'segment:1': {'intent': True, 'state': 'submission_unknown'}}})
                raise SubmissionUnknown('云端响应未知')
            assert rec['task_id'] == '123456789' and not rec['intent']
            return super().execute(job, ctx)
    fake = UnknownAdapter()
    app.state.engine.adapters['talking'] = fake
    identity = session(client)
    assert upload(client, identity, 'reference').status_code == 200
    proposal = plan(client, identity, '人物口播，文案：原任务继续。')
    created = execute(client, proposal['id']).json()
    wait_job(client, app, created['id'], ('submission_unknown',))
    route = f'/api/jobs/{created["id"]}/resume'
    assert client.post(route, json={}).status_code == 409
    assert client.post(route, json={'step_name': 'missing', 'task_id': '123456789'}).status_code == 409
    assert client.post(route, json={'step_name': 'segment:1', 'task_id': '123456789'}).status_code == 200
    final = wait_job(client, app, created['id'])
    assert final['details']['steps']['segment:1']['task_id'] == '123456789' and len(fake.calls) == 1


def test_completed_job_cannot_pause_resume_or_replace_remote_identity(studio):
    client, app, _, _ = studio
    _, proposal = product_plan(client)
    created = execute(client, proposal['id']).json()
    wait_job(client, app, created['id'])
    assert client.post(f'/api/jobs/{created["id"]}/pause').status_code == 409
    assert client.post(f'/api/jobs/{created["id"]}/resume', json={'remote_job_id': 'replacement'}).status_code == 409


def test_posts_require_current_token_and_request_ids_are_validated(studio):
    client, app, fake, _ = studio
    assert client.post('/api/sessions', headers={'x-app-token': 'wrong'}).status_code == 403
    _, proposal = product_plan(client)
    assert execute(client, proposal['id'], '../../invalid').status_code == 422
    assert fake.calls == [] and app.state.store.list('jobs') == []


@pytest.mark.parametrize(('role', 'filename', 'content'), [
    ('reference', 'fake.jpg', b'not an image'),
    ('product', 'empty.png', b''),
    ('config', 'secret.png', b'not authorized'),
])
def test_invalid_uploads_leave_no_saved_assets(studio, role, filename, content):
    client, app, _, settings = studio
    identity = session(client)
    response = upload(client, identity, role, filename, content)
    assert response.status_code == 400
    assert app.state.store.list('assets') == []
    assert not list((settings.data_dir / 'assets').glob('*'))


def test_upload_normalizes_image_and_confines_untrusted_filename(studio):
    client, app, _, settings = studio
    identity = session(client)
    response = upload(client, identity, 'reference', '../../outside.png')
    assert response.status_code == 200
    public = response.json()
    assert public['name'] == 'outside.png' and 'path' not in public
    stored = app.state.store.get('assets', public['id'])
    assert Path(stored['path']).is_relative_to(settings.data_dir / 'assets')
    downloaded = client.get(public['url'])
    assert downloaded.headers['x-content-type-options'] == 'nosniff'
    with Image.open(BytesIO(downloaded.content)) as image:
        assert image.mode == 'RGB' and image.getpixel((0, 0)) == (255, 255, 255)


def test_oversize_upload_and_artifact_traversal_are_rejected(studio, tmp_path):
    client, app, _, settings = studio
    identity = session(client)
    settings.max_upload_bytes = 8
    assert upload(client, identity).status_code == 413
    assert app.state.store.list('assets') == []
    settings.max_upload_bytes = 200 * 1024 * 1024
    assert upload(client, identity).status_code == 200
    proposal = plan(client, identity)
    created = execute(client, proposal['id']).json()
    wait_job(client, app, created['id'])
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'private file')
    app.state.store.update('jobs', created['id'], artifacts=[artifact(outside)])
    assert client.get(f'/api/jobs/{created["id"]}/artifacts/0').status_code == 404
    assert client.get(f'/api/jobs/{created["id"]}/artifacts/-1').status_code == 404
