"""Adapters for the two existing local, persistent video services.

The service job ID is the idempotency boundary. A failed poll never creates a
replacement job, and an ambiguous create request is never sent twice.
"""
from __future__ import annotations

from contextlib import ExitStack
import mimetypes
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests

from .base import AdapterError, ExecutionContext, NeedsReview, SubmissionUnknown, artifact


PRODUCT_OPTIONS = frozenset({
    'seconds', 'seed', 'aspect', 'sound', 'style', 'creative_mode', 'pipeline', 'notes',
})
MOTION_OPTIONS = frozenset({
    'workflow_id', 'positive_prompt', 'negative_prompt', 'motion_prompt',
    'segment_seconds', 'max_parallel_segments', 'generation_mode',
    'continuation_workflow_id', 'inner_segment_frames', 'split_strategy',
    'seam_transition_mode', 'flf2v_workflow_id', 'minimax_h3_workflow_id',
    'minimax_h3_profile', 'minimax_h3_prompt', 'minimax_h3_duration_seconds',
    'minimax_h3_seed_base', 'max_parallel_h3_transitions', 'legacy_h3_color_ramp_mode',
    'instance_type', 'use_personal_queue',
})
H3_ACTION_OPTIONS = frozenset({
    'workflow_id', 'max_parallel_segments', 'instance_type', 'use_personal_queue',
})
_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_PRODUCT_ACTIVE = {'queued', 'analyzing', 'uploading', 'submitting', 'waiting_queue',
                   'running', 'downloading', 'views_preparing', 'views_submitting',
                   'views_waiting_queue', 'views_running', 'views_downloading'}
_MOTION_ACTIVE = {'PENDING', 'SPLITTING', 'UPLOADING_REFERENCE', 'RUNNING',
                  'STITCHING', 'QUEUED', 'PROCESSING'}
_MOTION_STOPPED = {'FAILED', 'ERROR', 'CANCELLED', 'CANCELED', 'INTERRUPTED', 'PAUSED'}


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, '', 0, 'false', 'False', '0'):
        return False
    if value in (1, 'true', 'True', '1'):
        return True
    raise AdapterError('布尔选项无效')


def _fields(options: dict, allowed: frozenset) -> dict:
    result = {}
    for name in allowed:
        if name not in options or options[name] is None:
            continue
        value = options[name]
        if not isinstance(value, (str, int, float, bool)):
            raise AdapterError('生成选项需要为简单文本或数值')
        result[name] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def _base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
        parsed.port  # Validate port syntax before making a request.
    except (ValueError, TypeError):
        raise AdapterError('本地后端地址无效') from None
    return value.rstrip('/')


def _remote_id(value) -> str:
    if not isinstance(value, (str, int)) or not _ID.fullmatch(str(value)):
        raise AdapterError('本地后端没有返回有效任务编号')
    return str(value)


def _timeout(ctx: ExecutionContext) -> tuple[float, float]:
    seconds = float(ctx.settings.http_timeout)
    if seconds <= 0:
        raise AdapterError('HTTP 超时必须大于零')
    return min(10.0, seconds), seconds


def _get(session, url, ctx) -> dict:
    ctx.check_cancelled()
    try:
        response = session.get(url, timeout=_timeout(ctx), allow_redirects=False)
        with response:
            if not 200 <= response.status_code < 300:
                raise AdapterError(f'读取本地后端失败（HTTP {response.status_code}），任务编号已保留')
            result = response.json()
        if not isinstance(result, dict):
            raise AdapterError('本地后端返回的任务信息无效')
        return result
    except (requests.RequestException, ValueError):
        raise AdapterError('读取本地后端失败，已有任务可继续查询') from None


def _post(session, url, ctx, *, data=None, files=None, json=None, headers=None) -> dict:
    ctx.check_cancelled()
    ctx.update(submission_intent=True)
    try:
        response = session.post(
            url, data=data, files=files, json=json, headers=headers,
            timeout=_timeout(ctx), allow_redirects=False,
        )
        with response:
            if 400 <= response.status_code < 500:
                ctx.update(submission_intent=False)
                raise AdapterError(f'本地后端拒绝请求（HTTP {response.status_code}），请检查生成选项')
            if not 200 <= response.status_code < 300:
                raise SubmissionUnknown('本地后端提交结果不明确，请核对原服务任务，禁止重复创建')
            result = response.json()
        if not isinstance(result, dict):
            raise SubmissionUnknown('本地后端提交响应无效，请核对原服务任务')
        return result
    except (requests.RequestException, ValueError):
        raise SubmissionUnknown('本地后端提交响应丢失，请核对原服务任务，禁止重复创建') from None


def _download(session, url: str, destination: Path, ctx, *, label='成片', max_bytes=None, validate=None) -> None:
    """Read only the configured backend endpoint, never a returned media URL."""
    temporary = destination.with_suffix('.part')
    ctx.check_cancelled()
    try:
        with session.get(url, stream=True, timeout=_timeout(ctx), allow_redirects=False) as response:
            if not 200 <= response.status_code < 300:
                raise AdapterError(f'本地{label}下载失败（HTTP {response.status_code}），可继续下载原任务')
            size = 0
            with temporary.open('wb') as output:
                for chunk in response.iter_content(1024 * 1024):
                    ctx.check_cancelled()
                    if chunk:
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise AdapterError(f'{label}超过保存大小限制')
                        output.write(chunk)
            ctx.check_cancelled()
            if not size:
                raise AdapterError(f'本地{label}为空，可继续下载原任务')
            if validate:
                validate(temporary)
            temporary.replace(destination)
    except requests.RequestException:
        raise AdapterError(f'本地{label}下载中断，可继续下载原任务') from None
    finally:
        temporary.unlink(missing_ok=True)


def _multipart(stack: ExitStack, path: Path):
    return (path.name, stack.enter_context(path.open('rb')),
            mimetypes.guess_type(path.name)[0] or 'application/octet-stream')


def _details(job: dict, **patch) -> dict:
    # Copy only our own small metadata; never persist raw provider messages,
    # request headers, URLs, prompts, or error objects from these services.
    allowed = {'backend', 'backend_status', 'quality_enabled', 'quality_report_count',
               'quality_defect_count', 'quality_round', 'resume_requested', 'pipeline'}
    return {**{key: value for key, value in (job.get('details') or {}).items() if key in allowed}, **patch}


class ProductAdapter:
    def execute(self, job: dict, ctx: ExecutionContext) -> dict:
        base = _base_url(ctx.settings.product_url)
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        remote_id = job.get('remote_job_id')
        if not remote_id and job.get('submission_intent'):
            raise SubmissionUnknown('商品服务曾收到提交请求，请核对原任务编号，禁止重复创建')
        with requests.Session() as session:
            session.trust_env = False
            headers = {}
            if ctx.settings.rh_key:
                headers['X-RH-Key'] = ctx.settings.rh_key
            if ctx.settings.ds_key:
                headers['X-DS-Key'] = ctx.settings.ds_key
            if not remote_id:
                settings = _get(session, base + '/api/settings', ctx)
                token = settings.get('token')
                if not isinstance(token, str) or not token:
                    raise AdapterError('商品服务未返回会话令牌')
                headers['X-App-Token'] = token
                data = _fields(job.get('options') or {}, PRODUCT_OPTIONS)
                if data.get('creative_mode') == 'ad':
                    data['creative_mode'] = 'commercial'
                data['automatic'] = 'true'
                with ExitStack() as stack:
                    result = _post(session, base + '/api/jobs', ctx, data=data,
                                   files={'image': _multipart(stack, ctx.asset('image'))}, headers=headers)
                try:
                    remote_id = _remote_id(result.get('id'))
                except AdapterError:
                    raise SubmissionUnknown('商品服务未返回任务编号，请先核对原服务') from None
                job = ctx.update(remote_job_id=remote_id, details=_details(
                    job, backend='product', pipeline=data.get('pipeline', 'direct')))
            remote_id = _remote_id(remote_id)
            if (job.get('details') or {}).get('resume_requested'):
                original = _get(session, base + f'/api/jobs/{remote_id}', ctx)
                original_state = str(original.get('state', '')).lower()
                job = ctx.update(details=_details(job, resume_requested=False))
                if original_state == 'submission_unknown':
                    raise NeedsReview('请打开商品原工作台核对并绑定 RunningHub taskId，再继续查询原任务')
                if original_state in {'failed', 'interrupted', 'awaiting_review'}:
                    settings = _get(session, base + '/api/settings', ctx)
                    token = settings.get('token')
                    if not isinstance(token, str) or not token:
                        raise AdapterError('商品服务未返回会话令牌')
                    headers['X-App-Token'] = token
                    _post(session, base + f'/api/jobs/{remote_id}/continue', ctx, json={}, headers=headers)
                elif original_state not in _PRODUCT_ACTIVE | {'completed'}:
                    raise NeedsReview('商品服务返回未知状态，请打开原工作台核对')
            while True:
                current = _get(session, base + f'/api/jobs/{remote_id}', ctx)
                state = str(current.get('state', '')).lower()
                labels = {'queued': '商品任务排队中', 'analyzing': '正在识别商品并策划分镜',
                          'uploading': '正在准备商品参考图', 'submitting': '正在提交商品视频',
                          'waiting_queue': '等待云端视频队列', 'running': '云端正在生成商品视频',
                          'downloading': '正在下载商品视频', 'completed': '商品视频已完成，正在保存',
                          'views_preparing': '正在准备多视角参考图', 'views_submitting': '正在提交多视角出图',
                          'views_waiting_queue': '多视角图片任务排队中', 'views_running': '正在生成多视角图片',
                          'views_downloading': '正在下载多视角图片'}
                job = ctx.update(details=_details(job, backend_status=state[:60]),
                                 progress=labels.get(state, '正在核对商品任务状态'))
                if state == 'completed':
                    destination = ctx.workdir / 'product.mp4'
                    _download(session, base + f'/api/jobs/{remote_id}/video', destination, ctx)
                    return {'artifacts': [artifact(destination)], 'quality_status': 'not_checked',
                            'details': _details(job)}
                if state in ('failed', 'interrupted', 'submission_unknown', 'awaiting_review'):
                    raise NeedsReview('商品服务需要人工复核，请打开原工作台核对后显式继续；不会创建新任务')
                if state not in _PRODUCT_ACTIVE:
                    raise NeedsReview('商品服务返回未知状态，请在原任务核对')
                ctx.sleep(float(ctx.settings.poll_interval))


def _quality(task: dict, enabled: bool) -> tuple[str, dict]:
    reports = task.get('quality_audit_reports') or []
    reports = reports if isinstance(reports, list) else []
    latest = reports[-1] if reports and isinstance(reports[-1], dict) else None
    defects = latest.get('defects') if latest else None
    metadata = {'quality_enabled': enabled, 'quality_report_count': len(reports),
                'quality_defect_count': len(defects) if isinstance(defects, list) else 0}
    if not enabled:
        return 'not_checked', metadata
    # A clean final report supersedes findings from earlier rerun rounds.
    if isinstance(defects, list):
        return ('needs_review' if defects else 'passed'), metadata
    return 'needs_review', metadata


class MotionAdapter:
    def execute(self, job: dict, ctx: ExecutionContext) -> dict:
        base = _base_url(ctx.settings.motion_url)
        ctx.workdir.mkdir(parents=True, exist_ok=True)
        remote_id = job.get('remote_job_id')
        if not remote_id and job.get('submission_intent'):
            raise SubmissionUnknown('动作服务曾收到提交请求，请核对原任务编号，禁止重复创建')
        options = job.get('options') or {}
        backend = options.get('backend', 'wan')
        if backend not in ('wan', 'h3_action'):
            raise AdapterError('动作生成后端无效')
        enabled = _boolean(options.get('quality_enabled', False))
        with requests.Session() as session:
            session.trust_env = False
            keys = {}
            if ctx.settings.rh_key:
                keys['api_key'] = ctx.settings.rh_key
            if ctx.settings.ds_key:
                keys['deepseek_api_key'] = ctx.settings.ds_key
            if not remote_id:
                data = _fields(options, H3_ACTION_OPTIONS if backend == 'h3_action' else MOTION_OPTIONS)
                if data.get('seam_transition_mode') == 'minimax_h3_all':
                    data['seam_transition_mode'] = 'minimax_h3'
                data.update(keys)
                data['quality_audit_enabled'] = str(enabled).lower()
                if 'quality_max_rounds' in options:
                    data['quality_audit_max_rounds'] = str(options['quality_max_rounds'])
                path = '/api/minimax-h3-action-video-tasks' if backend == 'h3_action' else '/api/tasks'
                with ExitStack() as stack:
                    result = _post(session, base + path, ctx, data=data, files={
                        'reference_image': _multipart(stack, ctx.asset('reference')),
                        'driver_video': _multipart(stack, ctx.asset('driver')),
                    })
                task = result.get('task')
                try:
                    remote_id = _remote_id(task.get('id') if isinstance(task, dict) else None)
                except AdapterError:
                    raise SubmissionUnknown('动作服务未返回任务编号，请先核对原服务') from None
                job = ctx.update(remote_job_id=remote_id, details=_details(job, backend=backend, quality_enabled=enabled))
            remote_id = _remote_id(remote_id)
            if (job.get('details') or {}).get('resume_requested'):
                original = _get(session, base + f'/api/tasks/{remote_id}', ctx)
                original_status = str(original.get('status', '')).upper()
                job = ctx.update(details=_details(job, resume_requested=False))
                if original_status in _MOTION_STOPPED:
                    _post(session, base + f'/api/tasks/{remote_id}/resume', ctx, data=keys)
                elif original_status not in _MOTION_ACTIVE | {'SUCCESS'}:
                    raise NeedsReview('动作服务返回未知状态，请打开原工作台核对')
            while True:
                task = _get(session, base + f'/api/tasks/{remote_id}', ctx)
                status = str(task.get('status', '')).upper()
                labels = {'PENDING': '动作任务排队中', 'SPLITTING': '正在切分驱动视频',
                          'UPLOADING_REFERENCE': '正在上传人物及动作素材', 'RUNNING': '云端正在生成动作片段',
                          'STITCHING': '正在拼接动作视频或执行所选质检', 'SUCCESS': '动作视频已完成，正在保存'}
                job = ctx.update(details=_details(job, backend_status=status[:60]),
                                 progress=labels.get(status, '正在核对动作任务状态'))
                if status == 'SUCCESS':
                    audited = _boolean(task.get('quality_audit_enabled', enabled))
                    quality_status, metadata = _quality(task, audited)
                    destination = ctx.workdir / 'motion.mp4'
                    _download(session, base + f'/api/tasks/{remote_id}/download', destination, ctx)
                    return {'artifacts': [artifact(destination)], 'quality_status': quality_status,
                            'details': _details(job, **metadata)}
                if status in _MOTION_STOPPED:
                    raise NeedsReview('动作服务需要人工复核，请打开原工作台核对后显式继续；不会创建新任务')
                if status not in _MOTION_ACTIVE:
                    raise NeedsReview('动作服务返回未知状态，请在原任务核对')
                ctx.sleep(float(ctx.settings.poll_interval))
