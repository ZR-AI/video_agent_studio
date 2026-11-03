from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import fcntl
import json
import re
import secrets
import shutil
import subprocess
import threading

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
import requests

from .config import Settings, ROOT
from .engine import Engine
from .graph import VideoAgent
from .planner import LABELS, PlannerError
from .store import Store, new_id, now


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


def _probe(base, endpoint):
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(base + endpoint, timeout=(0.5, 1), allow_redirects=False)
        return response.status_code == 200
    except requests.RequestException:
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
        return [
            dict(id='product', label='商品广告', description='商品图生成广告、生活片或多视角特写', available=product,
                 reason='' if product else '请启动原商品视频服务（默认端口 8008）'),
            dict(id='talking', label='人物口播', description='人物图与文案生成分段口播，可选商品与音色参考', available=talking,
                 reason='' if talking else '需要配置 DeepSeek / RunningHub，并安装 ffmpeg'),
            dict(id='motion', label='动作迁移', description='人物图与驱动视频生成动作长视频', available=motion,
                 reason='' if motion else '请启动原动作视频服务（默认端口 8001）'),
        ]

    def public_asset(a):
        return {k: v for k, v in a.items() if k != 'path'} | {'url': f'/api/assets/{a["id"]}'}

    def public_job(job):
        result = dict(job)
        result['artifacts'] = [{k: v for k, v in a.items() if k != 'path'} |
                               {'url': f'/api/jobs/{job["id"]}/artifacts/{i}'}
                               for i, a in enumerate(job.get('artifacts') or [])]
        if job.get('remote_job_id'):
            base = settings.product_url if job['tool'] == 'product' else settings.motion_url
            result['backend_url'] = base + '/'
        return result

    def session_detail(identity):
        session = store.get('sessions', identity)
        return {**session, 'assets': [public_asset(a) for a in store.list('assets', identity)],
                'plan': store.get('plans', session['plan_id']) if session.get('plan_id') else None,
                'jobs': [public_job(j) for j in store.list('jobs', identity)]}

    def media_file(path, download=False):
        path = Path(path).resolve()
        if not path.is_relative_to(store.root) or not path.is_file():
            raise HTTPException(404, '素材文件不可用')
        return FileResponse(path, filename=path.name, content_disposition_type='attachment' if download else 'inline')

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'static/index.html')

    @app.get('/api/settings')
    def get_settings():
        return {'app_token': token, 'planner_mode': agent.planner.mode, 'tools': tools_status(),
                'limits': {'upload_mb': settings.max_upload_bytes // (1024 * 1024), 'product_seconds_max': 15},
                'version': '0.1.0', 'framework': 'LangGraph',
                'notice': '独立流程第一版；计划确认后才执行生成。暂停本地处理不会取消已提交的云端任务。'}

    @app.get('/api/sessions')
    def list_sessions():
        return [{k: s[k] for k in ('id', 'title', 'created_at', 'updated_at', 'plan_id')} for s in store.list('sessions')]

    @app.post('/api/sessions')
    def create_session():
        return store.create_session()

    @app.get('/api/sessions/{identity}')
    def get_session(identity: str):
        return session_detail(identity)

    @app.post('/api/sessions/{identity}/messages')
    def message(identity: str, body: Message):
        if body.tool not in ('auto', 'product', 'talking', 'motion') or not body.text.strip():
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
        if role not in {'product', 'reference', 'driver', 'last_frame', 'voice'}:
            raise HTTPException(400, '素材角色无效')
        identity = new_id()
        folder = store.root / 'assets'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = folder / (identity + '.upload')
        suffix = Path(file.filename or '').suffix.lower()
        image_role = role in {'product', 'reference', 'last_frame'}
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
                try:
                    with Image.open(temporary) as image:
                        if image.format not in {'PNG', 'JPEG', 'WEBP', 'GIF'} or image.width * image.height > 30_000_000:
                            raise ValueError
                        image.seek(0)
                        image = ImageOps.exif_transpose(image).convert('RGBA')
                        canvas = Image.new('RGBA', image.size, 'white')
                        canvas.alpha_composite(image)
                        canvas = canvas.convert('RGB')
                        canvas.thumbnail((2400, 2400))
                        canvas.save(destination, 'PNG')
                        metadata = {'width': canvas.width, 'height': canvas.height}
                except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                    raise HTTPException(400, '请上传有效的 JPG、PNG、WebP 或 GIF 图片') from None
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

    return app


def main():
    import uvicorn
    settings = Settings.load()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1)


if __name__ == '__main__':
    main()
