"""Exercise the cross-workflow compositor with real local PNG and MP4 media."""
from io import BytesIO
import json
import shutil
import subprocess

from PIL import Image, ImageDraw
import pytest

from video_agent_studio.adapters.base import artifact
from video_agent_studio.tests.test_image_projects import workbench
from video_agent_studio.tests.test_integration import plan, session, upload, wait_job


def completed(app, identity, tool, make_artifacts):
    store = app.state.store
    proposal = {'id': f'{identity}-{tool}', 'session_id': identity, 'tool': tool,
                'asset_ids': {}, 'options': {}}
    job = store.create_job(proposal, proposal['id'])
    folder = store.root / 'jobs' / job['id']
    folder.mkdir(parents=True)
    store.update('jobs', job['id'], state='completed', artifacts=make_artifacts(folder))


def scene_layers(folder):
    background = folder / 'room.png'
    foreground = folder / 'desk.png'
    whole = folder / 'whole.png'
    Image.new('RGB', (120, 160), 'blue').save(background)
    Image.new('RGB', (120, 160), 'white').save(whole)
    desk = Image.new('RGBA', (120, 160))
    ImageDraw.Draw(desk).rectangle((0, 115, 120, 160), fill='yellow')
    desk.save(foreground)
    return [
        {**artifact(whole, '直播间整图', 'image'), 'reuse_role': 'scene'},
        artifact(background, '干净背景', 'image'),
        artifact(foreground, '透明前景', 'image'),
    ]


def presenter(folder):
    portrait = folder / 'host.png'
    image = Image.new('RGB', (90, 120), 'lime')
    ImageDraw.Draw(image).rectangle((25, 25, 70, 118), fill='red')
    image.save(portrait)
    return portrait


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_preview_video_and_export_reuse_layers_with_audio(workbench):
    client, app, _, _ = workbench
    identity = session(client)
    other = session(client)
    endpoint = f'/api/sessions/{identity}/composition'
    completed(app, identity, 'background', scene_layers)
    completed(app, identity, 'qwen_image', lambda folder: [artifact(presenter(folder), '造型图', 'image')])

    def talking(folder):
        path = folder / 'talk.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-loop', '1', '-i', str(presenter(folder)),
                        '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1',
                        '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=10)
        return [artifact(path, '口播成片')]

    completed(app, identity, 'talking', talking)
    completed(app, identity, 'motion', talking)
    catalog = client.get(endpoint).json()
    assert len(catalog['scenes']) == 1
    assert {item['label'] for item in catalog['people']} == {'口播成片', '动作迁移成片', '造型图'}
    assert client.get(f'/api/sessions/{other}/composition').json() == {'scenes': [], 'people': []}

    options = {'scene': catalog['scenes'][0]['id'],
               'person': next(item['id'] for item in catalog['people'] if item['kind'] == 'image')}
    assert client.post(endpoint + '/export', json=options).status_code == 422
    still = client.post(endpoint + '/preview', json=options)
    assert still.is_success and still.headers['x-matte-usable'] == 'true'
    bad_key = client.post(endpoint + '/preview', json={**options, 'color': '#0000ff'})
    assert bad_key.is_success and bad_key.headers['x-matte-usable'] == 'false'

    for name in ('口播成片', '动作迁移成片'):
        options['person'] = next(item['id'] for item in catalog['people'] if item['label'] == name)
        assert client.post(f'/api/sessions/{other}/composition/export', json=options).status_code == 422
        frame = client.post(endpoint + '/preview', json={**options, 'seconds': 0.5})
        assert frame.is_success and frame.headers['x-matte-usable'] == 'true'
        with Image.open(BytesIO(frame.content)) as image:
            blue = image.getpixel((15, 55))
            red = image.getpixel((60, 78))
            yellow = image.getpixel((60, 130))
            assert blue[2] > 220 and red[0] > 220 and max(red[1:3]) < 35
            assert min(yellow[:2]) > 220 and yellow[2] < 35

        result = client.post(endpoint + '/export', json=options)
        assert result.is_success, result.text
        job = wait_job(client, app, result.json()['id'])
        assert client.post(endpoint + '/export', json=options).json()['id'] == job['id']
        downloaded = client.get(job['artifacts'][0]['url'] + '?download=true')
        assert downloaded.headers['content-type'] == 'video/mp4'
        path = app.state.store.get('jobs', job['id'])['artifacts'][0]['path']
        info = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height,duration',
                               '-of', 'json', path], capture_output=True, check=True, timeout=10)
        video, audio = json.loads(info.stdout)['streams']
        assert audio['codec_type'] == 'audio'
        assert (video['width'], video['height'], float(video['duration'])) == (120, 160, 1.0)


def test_talking_can_keep_product_in_scene_without_holding_it(workbench):
    client, _, _, _ = workbench
    identity = session(client)
    assert upload(client, identity, 'reference').is_success
    assert upload(client, identity, 'product').is_success
    original = plan(client, identity, '口播文案：大家好', tool='talking')
    assert original['ready'] and 'product' in original['asset_ids']
    greenscreen = plan(client, identity, '保留绿幕，主播不手持商品。口播文案：大家好', tool='talking')
    assert greenscreen['ready'] and greenscreen['options']['script'] == '大家好'
    assert set(greenscreen['asset_ids']) == {'reference'}
