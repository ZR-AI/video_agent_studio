"""Run the original image workbenches through their local HTTP APIs."""
from __future__ import annotations

from io import BytesIO
import re
import time

from PIL import Image
import requests

from .base import AdapterError, ExecutionContext, SubmissionUnknown, artifact
from .http_projects import _base_url, _download, _get, _post, _timeout


QWEN_SLOTS = {'portrait': 'reference', 'costume': 'costume', 'pose': 'pose', 'hair': 'hair'}
_UUID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}')
_PORTRAIT_ID = re.compile(r'[0-9a-f]{32}')
_PORTRAIT_IMAGE = re.compile(r'image-[1-9][0-9]*\.png')
_PORTRAIT_FIELDS = ('style', 'background', 'aspect', 'notes', 'custom_style', 'custom_color',
                    'pipeline', 'region', 'custom_region', 'count')


def _session():
    session = requests.Session()
    session.trust_env = False
    return session


def valid_qwen_id(value: str) -> bool:
    return bool(_UUID.fullmatch(value))


def _job_ids(values, pattern, minimum, maximum):
    if (not isinstance(values, list) or not minimum <= len(values) <= maximum
            or any(not isinstance(value, str) or not pattern.fullmatch(value) for value in values)
            or len(set(values)) != len(values)):
        raise SubmissionUnknown('生图服务未返回有效的任务编号，请核对原任务')
    return values


def _validate_png(path):
    try:
        with Image.open(path, formats=['PNG']) as image:
            image.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise AdapterError('服务返回的图片无效，可恢复后重新下载原任务') from None


def _save_image(session, url, ctx, filename, label):
    path = ctx.workdir / filename
    if not path.is_file():
        _download(session, url, path, ctx, label='图片', max_bytes=64 * 1024 * 1024, validate=_validate_png)
    return artifact(path, label, 'image')


def _poll_batch(session, base, ids, details, ctx, label, inspect):
    """Poll known jobs and retain successful images even if other items fail."""
    deadline = time.monotonic() + 35 * 60
    pending = dict(enumerate(ids, 1))
    images, failed = {}, []
    while pending and time.monotonic() < deadline:
        for position, remote_id in list(pending.items()):
            endpoint = f'{base}/api/jobs/{remote_id}'
            current = _get(session, endpoint, ctx)
            state, output = inspect(current, endpoint, position)
            if state == 'done':
                images[position] = output
                del pending[position]
            elif state == 'failed':
                failed.append(f'第 {position} 张：{output}')
                del pending[position]
            artifacts = [item for index in sorted(images) for item in images[index]]
            ctx.update(artifacts=artifacts, details={**details, 'completed_count': len(images)},
                       progress=f'{label}：{len(ids) - len(pending)}/{len(ids)} 个任务已结束')
        if pending:
            ctx.sleep(ctx.settings.poll_interval)
    if pending:
        raise AdapterError('等待图片超时，恢复后继续查询原任务')
    message = '；'.join(filter(None, [details.get('batch_warning'), *failed]))
    if message:
        raise AdapterError(message)
    return {'artifacts': artifacts, 'quality_status': 'not_checked',
            'details': {**details, 'completed_count': len(images)}}


class QwenImageAdapter:
    def execute(self, job: dict, ctx: ExecutionContext) -> dict:
        base = _base_url(ctx.settings.qwen_image_url)
        details = dict(job.get('details') or {})
        details.pop('resume_requested', None)
        options = job['options']
        with _session() as session:
            ids = details.get('batch_ids') or ([job['remote_job_id']] if job.get('remote_job_id') else [])
            if not ids:
                if job.get('submission_intent'):
                    raise SubmissionUnknown('本地造型任务可能已接单，请补录造型工作台的任务编号')
                randomize = options.get('randomize', [])
                refs = {}
                for slot, role in QWEN_SLOTS.items():
                    path = ctx.asset(role, required=False) if slot not in randomize else None
                    if path:
                        refs[slot] = {'upload': self._upload(session, base, path, ctx)}
                payload = {key: options[key] for key in ('model', 'instruction', 'ratio', 'count', 'parallel', 'randomize')}
                ctx.update(progress='正在提交本地造型工作流')
                result = _post(session, f'{base}/api/jobs', ctx, json={**payload, 'references': refs})
                rows = result.get('jobs')
                ids = _job_ids([row.get('id') if isinstance(row, dict) else None for row in rows]
                               if isinstance(rows, list) else [result.get('id')], _UUID, 1, payload['count'])
                if len(ids) < payload['count']:
                    details['batch_warning'] = f'原服务仅确认提交了 {len(ids)}/{payload["count"]} 张，其余图片未自动补交。'
            ids = _job_ids(ids, _UUID, 1, 8)
            if len(ids) < options['count']:
                details.setdefault('batch_warning', f'已绑定 {len(ids)}/{options["count"]} 张，请在造型工作台核对同批的其余任务。')
            details['batch_ids'] = ids
            ctx.update(remote_job_id=ids[0], details=details)

            def inspect(current, endpoint, position):
                state = current.get('state')
                if state == 'success':
                    image = _save_image(session, f'{endpoint}/image', ctx, f'qwen-{position}.png', f'造型图 {position}')
                    return 'done', [image]
                if state in {'cancelled', 'error'}:
                    return 'failed', '本地任务已取消或生成失败，请在造型工作台查看详情'
                if state not in {'queued', 'running'}:
                    raise AdapterError('造型工作台返回未知状态，请在原服务核对')
                return 'pending', None

            return _poll_batch(session, base, ids, details, ctx, '本地造型', inspect)

    @staticmethod
    def _upload(session, base, path, ctx):
        ctx.check_cancelled()
        ctx.update(progress='正在上传造型参考图')
        try:
            buffer = BytesIO()
            with Image.open(path) as image:
                image.thumbnail((2048, 2048))
                image.convert('RGB').save(buffer, 'JPEG', quality=96)
            with session.post(f'{base}/api/uploads', data=buffer.getvalue(), headers={'Content-Type': 'image/jpeg'},
                              timeout=_timeout(ctx), allow_redirects=False) as response:
                if response.status_code != 201:
                    raise AdapterError(f'造型参考图上传失败（HTTP {response.status_code}）')
                identifier = response.json().get('upload')
                if not isinstance(identifier, str) or not _UUID.fullmatch(identifier):
                    raise ValueError
        except (requests.RequestException, OSError, ValueError, AttributeError):
            raise AdapterError('无法上传造型参考图，请检查素材和造型工作台连接') from None
        return identifier


class PortraitImageAdapter:
    def execute(self, job: dict, ctx: ExecutionContext) -> dict:
        base = _base_url(ctx.settings.portrait_image_url)
        details = dict(job.get('details') or {})
        resume = details.pop('resume_requested', False)
        count = job['options']['count']
        with _session() as session:
            def headers():
                token = _get(session, f'{base}/api/settings', ctx).get('token')
                if not isinstance(token, str) or not token:
                    raise AdapterError('人像灵感工坊未返回有效的会话令牌')
                return {'X-App-Token': token}

            ids = details.get('batch_ids')
            if not ids:
                body = {key: job['options'][key] for key in _PORTRAIT_FIELDS if key in job['options']}
                # The upstream stores this UUID and all child IDs in one transaction.
                # A lost response can be recovered by using the same UUID and options.
                try:
                    result = _post(session, f'{base}/api/jobs', ctx, headers=headers(),
                                   json={**body, 'request_id': job['id'], 'auto_generate': True})
                    ids = _job_ids(result.get('batch_ids'), _PORTRAIT_ID, count, count)
                    if ids[0] != job['id']:
                        raise SubmissionUnknown('人像批次编号与当前请求不一致')
                except SubmissionUnknown:
                    raise AdapterError('人像批次回执未确认。恢复原任务将用同一个请求编号取回批次') from None
            ids = _job_ids(ids, _PORTRAIT_ID, count, count)
            details['batch_ids'] = ids
            ctx.update(remote_job_id=ids[0], details=details)
            checked_resume = set()

            def inspect(current, endpoint, position):
                state = current.get('status')
                if state == 'completed':
                    names = current.get('images')
                    if (not isinstance(names, list) or not names or len(names) > 10
                            or any(not isinstance(name, str) or not _PORTRAIT_IMAGE.fullmatch(name) for name in names)):
                        raise AdapterError('人像灵感工坊未返回有效的图片列表')
                    images = [_save_image(session, f'{endpoint}/images/{name}', ctx, f'portrait-{position}-{index}.png',
                                          f'人像 {position}' if len(names) == 1 else f'人像 {position} · {index}')
                              for index, name in enumerate(names, 1)]
                    return 'done', images
                if state == 'unknown':
                    return 'failed', '请在原人像灵感工坊核对并绑定 RunningHub taskId，再恢复本任务'
                if state in {'failed', 'paused', 'preview'} and not current.get('active'):
                    if state == 'failed' and current.get('task_id'):
                        return 'failed', 'RunningHub 已确认失败，请在原工作台查看详情'
                    if resume and position not in checked_resume and current.get('resumable') is not False:
                        checked_resume.add(position)
                        try:
                            _post(session, f'{endpoint}/continue', ctx, headers=headers(), json={})
                        except SubmissionUnknown:
                            raise AdapterError('继续请求未确认，请恢复任务以查询原批次') from None
                        return 'pending', None
                    return 'failed', '生成已中断，请恢复原人像任务'
                if state not in {'queued', 'planning', 'waiting', 'submitting', 'running', 'downloading', 'paused', 'failed', 'preview'}:
                    raise AdapterError('人像灵感工坊返回未知状态，请在原服务核对')
                return 'pending', None

            return _poll_batch(session, base, ids, details, ctx, '人像生成', inspect)
