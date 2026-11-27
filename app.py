from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from threading import BoundedSemaphore
import fcntl
import json
import re
import secrets
import shutil
import subprocess
import threading

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
import requests

from .config import Settings, ROOT
from .composition import CompositionOptions, CompositionPreview, preview, resolve, sources
from .adapters.base import AdapterError
from .adapters.image_projects import QWEN_SLOTS
from .engine import Engine
from .graph import VideoAgent
from .planner import LABELS, PlannerError
from .store import Store, new_id, now

SELECTABLE_TOOLS = {'auto', *LABELS} - {'clarify'}


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=18000)
    tool: str = 'auto'


class Execute(BaseModel):
    request_id: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


class Resume(BaseModel):
    frames_approved: bool = False
    remote_job_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,128}$')
    step_name: str | None = Field(default=None, pattern=r'^[A-Za-z0-9:_-]{1,128}$')
    task_id: str | None = Field(default=None, pattern=r'^\d{1,128}$')


def _normalize_image(source: Path, destination: Path, *, max_side: int = 2400) -> dict:
    try:
        with Image.open(source) as image:
            if image.format not in {'PNG', 'JPEG', 'WEBP', 'GIF'} or image.width * image.height > 30_000_000:
                raise ValueError
            rgba = ImageOps.exif_transpose(image).convert('RGBA')
            rgba.thumbnail((max_side, max_side))
            canvas = Image.new('RGB', rgba.size, 'white')
            canvas.paste(rgba, mask=rgba.getchannel('A'))
            canvas.save(destination, 'PNG')
            return {'width': canvas.width, 'height': canvas.height}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(400, '请提供有效的 JPG、PNG、WebP 或 GIF 图片') from None


def _artifact_image_key(item: dict) -> str | None:
    path = item.get('path')
    if item.get('kind') == 'image' and isinstance(path, str) and path:
        # Batch results may be reordered as earlier images finish. The source
        # path identifies the chosen image even when its list index changes.
        return sha256(path.encode()).hexdigest()
    return None


def _probe(base, endpoint, *, required_fields=()):
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(base + endpoint, timeout=(0.5, 1), allow_redirects=False)
        if response.status_code != 200:
            return False
        if required_fields:
            payload = response.json()
            return isinstance(payload, dict) and all(payload.get(key) is True for key in required_fields)
        return True
    except (requests.RequestException, ValueError):
        return False
    finally:
        session.close()


def create_app(settings: Settings | None = None, *, planner=None, adapters=None, check_tools=True):
    settings = settings or Settings.load()
    store = Store(settings.data_dir)
    agent = VideoAgent(store, settings, planner)
    engine = Engine(store, settings, adapters)
    token = secrets.token_urlsafe(32)
    session_lock = threading.RLock()
    preview_slots = BoundedSemaphore(2)

    @asynccontextmanager
    async def lifespan(app):
        lockfile = (store.root / '.server.lock').open('a')
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lockfile.close()
            raise RuntimeError('此数据目录已有服务运行；请使用单个 worker') from None
        store.recover()
        engine.kick()
        try:
            yield
        finally:
            engine.close()
            agent.close()
            lockfile.close()

    app = FastAPI(title='视频 Agent 工作台', lifespan=lifespan)
    app.state.store, app.state.agent, app.state.engine = store, agent, engine
    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

    @app.middleware('http')
    async def guard(request: Request, call_next):
        if request.method == 'POST':
            if not secrets.compare_digest(request.headers.get('x-app-token', ''), token):
                return JSONResponse({'detail': '连接已更新，请刷新页面后重试'}, status_code=403)
            try:
                if int(request.headers.get('content-length', '0')) > settings.max_upload_bytes + 1024 * 1024:
                    return JSONResponse({'detail': '请求超过上传限制'}, status_code=413)
            except ValueError:
                return JSONResponse({'detail': '请求长度无效'}, status_code=400)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({'detail': '记录不存在'}, status_code=404)

    def tools_status():
        product = not check_tools or _probe(settings.product_url, '/api/settings')
        motion = not check_tools or _probe(settings.motion_url, '/api/config')
        talking = not check_tools or (bool(settings.rh_key) and bool(settings.ds_key or settings.fl2va_url) and bool(shutil.which('ffmpeg')))
        qwen_image = not check_tools or _probe(settings.qwen_image_url, '/api/health', required_fields=('connected',))
        portrait_image = not check_tools or _probe(settings.portrait_image_url, '/api/settings',
                                                   required_fields=('deepseek_configured', 'runninghub_configured'))
        background = not check_tools or _probe(settings.background_url, '/api/meta')
        return [
            dict(id='product', label='商品广告', description='商品图生成广告、生活片或多视角特写', available=product,
                 reason='' if product else '请启动原商品视频服务（默认端口 8008）'),
            dict(id='talking', label='人物口播', description='人物图与文案生成分段口播，可选商品与音色参考', available=talking,
                 reason='' if talking else '需要配置 DeepSeek / RunningHub，并安装 ffmpeg'),
            dict(id='motion', label='动作迁移', description='人物图与驱动视频生成动作长视频', available=motion,
                 reason='' if motion else '请启动原动作视频服务（默认端口 8001）'),
            dict(id='qwen_image', label='参考图造型', description='在本机用 Qwen 或 H3 Image 换装、换姿势或改发型',
                 available=qwen_image, reason='' if qwen_image else '请检查造型工作台（8190）和本机 ComfyUI'),
            dict(id='portrait_image', label='文字生人像', description='按文字生成坐姿正面半身人像，支持批量出图',
                 available=portrait_image, reason='' if portrait_image else '请检查人像灵感工坊（8009）和服务的 API 配置'),
            dict(id='background', label='直播布景', description='用商品图生成直播间整图和分层素材',
                 available=background, reason='' if background else '请检查 LiveBG 服务及 BACKGROUND_STUDIO_URL 配置'),
        ]

    def public_asset(a):
        return {k: v for k, v in a.items() if k != 'path'} | {'url': f'/api/assets/{a["id"]}'}

    def public_job(job):
        result = dict(job)
        artifacts_url = f'/api/jobs/{job["id"]}/artifacts'
        result['artifacts'] = []
        for index, item in enumerate(job.get('artifacts') or []):
            public = {key: value for key, value in item.items() if key != 'path'}
            public['url'] = f'{artifacts_url}/{index}'
            image_key = _artifact_image_key(item)
            if image_key:
                public['url'] = f'{artifacts_url}/image/{image_key}'
                role = item.get('reuse_role', 'reference')
                if role in ('reference', 'scene'):
                    public['reference_url'] = f'{artifacts_url}/{image_key}/reference'
            result['artifacts'].append(public)
        backend_urls = {'product': settings.product_url, 'motion': settings.motion_url,
                        'qwen_image': settings.qwen_image_url, 'portrait_image': settings.portrait_image_url,
                        'background': settings.background_url}
        base = backend_urls.get(job['tool'])
        if base and (job.get('remote_job_id') or job.get('submission_intent')):
            result['backend_url'] = base + '/'
        return result

    def session_detail(identity):
        session = store.get('sessions', identity)
        return {**session, 'assets': [public_asset(a) for a in store.list('assets', identity)],
                'plan': store.get('plans', session['plan_id']) if session.get('plan_id') else None,
                'jobs': [public_job(j) for j in store.list('jobs', identity)]}

    def media_path(path):
        path = Path(path).resolve()
        if not path.is_relative_to(store.root) or not path.is_file():
            raise HTTPException(404, '素材文件不可用')
        return path

    def media_file(path, download=False):
        path = media_path(path)
        return FileResponse(path, filename=path.name, content_disposition_type='attachment' if download else 'inline')

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'static/index.html')

    @app.get('/api/settings')
    def get_settings():
        return {'app_token': token, 'planner_mode': agent.planner.mode, 'tools': tools_status(),
                'composition_available': bool(shutil.which('ffmpeg') and shutil.which('ffprobe')),
                'limits': {'upload_mb': settings.max_upload_bytes // (1024 * 1024), 'product_seconds_max': 15},
                'version': '0.2.0', 'framework': 'LangGraph',
                'notice': '每个计划执行一种图片或视频工作流。暂停工作台任务后，已提交的生成仍可在原服务继续。'}

    @app.get('/api/sessions')
    def list_sessions():
        return [{k: s[k] for k in ('id', 'title', 'created_at', 'updated_at', 'plan_id')} for s in store.list('sessions')]

    @app.post('/api/sessions')
    def create_session():
        return store.create_session()

    @app.get('/api/sessions/{identity}')
    def get_session(identity: str):
        return session_detail(identity)

    @app.get('/api/sessions/{identity}/composition')
    def composition_sources(identity: str):
        store.get('sessions', identity)
        scenes, people = sources(store, identity)
        return {'scenes': [dict(id=key, label=item.label, date=item.date) for key, item in scenes.items()],
                'people': [dict(id=key, label=item.label, date=item.date, kind=item.kind) for key, item in people.items()]}

    @app.post('/api/sessions/{identity}/composition/preview')
    def composition_preview(identity: str, body: CompositionPreview):
        store.get('sessions', identity)
        if not preview_slots.acquire(blocking=False):
            raise HTTPException(429, '预览正在生成，请稍候再试')
        try:
            result, usable = preview(*resolve(store, identity, body), body)
        except AdapterError as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            preview_slots.release()
        return Response(result, media_type='image/png', headers={'X-Matte-Usable': str(usable).lower()})

    @app.post('/api/sessions/{identity}/composition/export')
    def composition_export(identity: str, body: CompositionOptions):
        store.get('sessions', identity)
        try:
            _, person = resolve(store, identity, body)
        except AdapterError as exc:
            raise HTTPException(422, str(exc)) from None
        if person.kind != 'video':
            raise HTTPException(422, '导出成片需要人物口播或动作迁移的视频')
        digest = sha256((identity + ':' + body.model_dump_json()).encode()).hexdigest()
        plan = {'id': 'composition-' + digest, 'session_id': identity, 'tool': 'composition', 'title': '直播间合成',
                'options': body.model_dump(), 'asset_ids': {}, 'ready': True, 'created_at': now()}
        store.put('plans', plan)
        job = store.create_job(plan, plan['id'])
        engine.kick()
        return public_job(job)

    @app.post('/api/sessions/{identity}/messages')
    def message(identity: str, body: Message):
        if body.tool not in SELECTABLE_TOOLS or not body.text.strip():
            raise HTTPException(400, '请选择有效工具并填写需求')
        with session_lock:
            session = store.get('sessions', identity)
            try:
                plan, reply = agent.plan(session, store.list('assets', identity), body.text.strip(), body.tool)
            except PlannerError as exc:
                raise HTTPException(502, str(exc)) from None
            return {'session': session_detail(identity), 'plan': plan, 'reply': reply}

    @app.get('/api/plans/{identity}')
    def get_plan(identity: str):
        return store.get('plans', identity)

    @app.post('/api/plans/{identity}/execute')
    def execute(identity: str, body: Execute):
        plan = store.get('plans', identity)
        # An idempotent repeat returns its existing job even if a backend is now offline.
        existing = next((j for j in store.list('jobs', plan['session_id']) if j['plan_id'] == identity), None)
        if existing:
            return public_job(existing)
        tool = next((t for t in tools_status() if t['id'] == plan['tool']), None)
        if not tool or not tool['available']:
            raise HTTPException(409, (tool or {}).get('reason') or '计划尚未选择可用工具')
        try:
            job = agent.execute(identity, body.request_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        engine.kick()
        return public_job(job)

    @app.post('/api/assets')
    async def upload_asset(file: UploadFile = File(...), role: str = Form(...), session_id: str = Form(...)):
        store.get('sessions', session_id)
        image_roles = {'product', 'last_frame', 'scene', *QWEN_SLOTS.values()}
        if role not in image_roles | {'driver', 'voice'}:
            raise HTTPException(400, '素材角色无效')
        identity = new_id()
        folder = store.root / 'assets'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = folder / (identity + '.upload')
        suffix = Path(file.filename or '').suffix.lower()
        image_role = role in image_roles
        allowed = {'.mp4', '.mov', '.mkv', '.avi', '.webm'} if role == 'driver' else {'.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac'}
        destination = folder / (identity + ('.png' if image_role else suffix))
        size, metadata = 0, {}
        try:
            with temporary.open('wb') as output:
                while data := await file.read(1024 * 1024):
                    size += len(data)
                    if size > min(settings.max_upload_bytes, 12 * 1024 * 1024 if image_role else settings.max_upload_bytes):
                        raise HTTPException(413, '素材超过大小限制；图片最大 12 MB，其余素材最大 200 MB')
                    output.write(data)
            if not size:
                raise HTTPException(400, '上传文件为空')
            if image_role:
                metadata = _normalize_image(temporary, destination, max_side=2048 if role == 'scene' else 2400)
                if role == 'scene' and min(metadata.values()) < 64:
                    raise HTTPException(400, '布景参考图的宽和高都须至少 64 像素')
                kind = 'image'
            else:
                if suffix not in allowed:
                    raise HTTPException(400, '素材文件扩展名不支持')
                try:
                    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(temporary)],
                                           check=True, capture_output=True, timeout=20)
                    info = json.loads(probe.stdout)
                    expected = 'video' if role == 'driver' else 'audio'
                    if not any(s.get('codec_type') == expected for s in info.get('streams', [])):
                        raise ValueError
                    metadata = {'duration': float(info.get('format', {}).get('duration') or 0)}
                except (OSError, subprocess.SubprocessError, ValueError):
                    raise HTTPException(400, '无法读取音视频；请确认文件有效且本机已安装 ffprobe') from None
                temporary.replace(destination)
                kind = expected
            asset = dict(id=identity, session_id=session_id, role=role, kind=kind,
                         name=Path(file.filename or '素材').name[:160], path=str(destination), size=destination.stat().st_size,
                         created_at=now(), **metadata)
            store.put('assets', asset)
            return public_asset(asset)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
            await file.close()

    @app.get('/api/assets/{identity}')
    def get_asset(identity: str, download: bool = False):
        return media_file(store.get('assets', identity)['path'], download)

    @app.get('/api/jobs')
    def list_jobs():
        return [public_job(j) for j in store.list('jobs')]

    @app.get('/api/jobs/{identity}')
    def get_job(identity: str):
        return public_job(store.get('jobs', identity))

    @app.post('/api/jobs/{identity}/pause')
    def pause(identity: str):
        try:
            return public_job(engine.pause(identity))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post('/api/jobs/{identity}/resume')
    def resume(identity: str, body: Resume):
        try:
            return public_job(engine.resume(identity, **body.model_dump()))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/api/jobs/{identity}/artifacts/{index}')
    def get_artifact(identity: str, index: int, download: bool = False):
        artifacts = store.get('jobs', identity).get('artifacts') or []
        if not 0 <= index < len(artifacts):
            raise HTTPException(404, '成片或参考帧不存在')
        return media_file(artifacts[index]['path'], download)

    def find_artifact_image(job: dict, image_key: str) -> dict:
        image = next((item for item in (job.get('artifacts') or []) if _artifact_image_key(item) == image_key), None)
        if image is None:
            raise HTTPException(404, '结果图不可用，请刷新任务后再选')
        return image

    @app.get('/api/jobs/{identity}/artifacts/image/{image_key}')
    def get_artifact_image(identity: str, image_key: str, download: bool = False):
        image = find_artifact_image(store.get('jobs', identity), image_key)
        return media_file(image['path'], download)

    @app.post('/api/jobs/{identity}/artifacts/{image_key}/reference')
    def use_as_reference(identity: str, image_key: str):
        with session_lock:
            job = store.get('jobs', identity)
            image = find_artifact_image(job, image_key)
            role = image.get('reuse_role', 'reference')
            if role not in ('reference', 'scene'):
                raise HTTPException(400, '此图片不支持直接设为参考图')
            session_id = job['session_id']
            references = [item for item in store.list('assets', session_id) if item['role'] == role]
            reference_url = f'/api/jobs/{identity}/artifacts/{image_key}/reference'
            existing = next((item for item in references if item.get('source_reference_url') == reference_url), None)
            if existing and Path(existing['path']).is_file():
                # Moving the existing record to the newest position makes it the
                # default for future plans while existing plans keep their IDs.
                if existing['id'] != references[0]['id']:
                    store.put('assets', existing)
                return public_asset(existing)

            source = media_path(image['path'])
            asset_id = existing['id'] if existing else new_id()
            destination = store.root / 'assets' / f'{asset_id}.png'
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                # A 1920px RGB PNG fits the downstream 12 MB upload limit.
                metadata = _normalize_image(source, destination, max_side=2048 if role == 'scene' else 1920)
                if role == 'scene' and min(metadata.values()) < 64:
                    raise HTTPException(400, '布景参考图的宽和高都须至少 64 像素')
                asset = {'id': asset_id, 'session_id': session_id, 'role': role, 'kind': 'image',
                         'name': f'{image.get("label") or "结果图"} · {identity[:8]}.png',
                         'path': str(destination), 'size': destination.stat().st_size,
                         'created_at': now(), 'source_reference_url': reference_url, **metadata}
                return public_asset(store.put('assets', asset))
            except Exception:
                destination.unlink(missing_ok=True)
                raise

    return app


def main():
    import uvicorn
    settings = Settings.load()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1)


if __name__ == '__main__':
    main()
