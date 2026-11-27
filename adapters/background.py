"""Run LiveBG and save the scene and its layers in the Agent workspace."""
from __future__ import annotations

from contextlib import ExitStack
import re
import time

import requests

from .base import AdapterError, ExecutionContext, SubmissionUnknown
from .http_projects import _base_url, _get, _multipart, _post, _timeout
from .image_projects import _save_image, _session


def valid_background_id(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'\d{4}_\d{6}_[a-f0-9]{4}', value, re.ASCII))


def _upload(session, url, files, ctx, *, label, id_length, data=None):
    ctx.check_cancelled()
    try:
        with session.post(url, files=files, data=data, timeout=_timeout(ctx), allow_redirects=False) as response:
            if not 200 <= response.status_code < 300:
                raise AdapterError(f'{label}上传失败（HTTP {response.status_code}）')
            payload = response.json()
    except (requests.RequestException, ValueError):
        raise AdapterError(f'{label}上传失败，请检查直播布景服务后恢复') from None
    identity = payload.get('id') if isinstance(payload, dict) else None
    if not isinstance(identity, str) or not re.fullmatch(rf'[a-f0-9]{{{id_length}}}', identity):
        raise AdapterError(f'直播布景服务未返回有效的{label}编号')
    return identity


class BackgroundAdapter:
    def execute(self, job: dict, ctx: ExecutionContext) -> dict:
        base = _base_url(ctx.settings.background_url)
        options = job['options']
        mode = options['scene_mode']
        identity = job.get('remote_job_id')
        with _session() as session:
            if not identity:
                if job.get('submission_intent'):
                    raise SubmissionUnknown('请在直播布景原工作台的历史记录核对任务编号')
                identity = self._submit(session, base, mode, options, ctx)
            if not valid_background_id(identity):
                raise AdapterError('直播布景任务编号无效')

            deadline = time.monotonic() + 45 * 60
            while time.monotonic() < deadline:
                remote = _get(session, f'{base}/api/job/{identity}', ctx)
                if remote.get('id') != identity or remote.get('scene_mode') != mode:
                    raise AdapterError('原任务编号或布景方式与当前计划不匹配')
                status = remote.get('status')
                if status in ('done', 'partial', 'error', 'cancelled'):
                    return self._collect(session, base, identity, remote, options, ctx)
                if status not in ('queued', 'running'):
                    raise AdapterError('直播布景服务返回了未知任务状态')
                progress = remote.get('progress')
                text = '直播布景排队中' if status == 'queued' else '原工作台正在生成布景'
                if type(progress) in (int, float) and 0 <= progress <= 100:
                    text += f' · {progress:.0f}%'
                ctx.update(progress=text)
                ctx.sleep(ctx.settings.poll_interval)
            raise AdapterError('等待直播布景超时，可恢复查询同一个任务')

    def _submit(self, session, base, mode, options, ctx):
        ctx.update(progress='正在上传布景素材')
        with ExitStack() as stack:
            products = [ctx.asset('product' if i == 1 else f'product_{i}') for i in range(1, options['product_count'] + 1)]
            scene = ctx.asset('scene') if mode == 'reference' else None
            files = [('files', _multipart(stack, path)) for path in products]
            product_id = _upload(session, f'{base}/api/upload', files, ctx, label='商品图', id_length=10,
                                 data={'scene_mode': mode})
            payload = {'id': product_id, 'scene_mode': mode, 'lang': options['lang']}
            if scene:
                scene_id = _upload(session, f'{base}/api/scene-reference', {'file': _multipart(stack, scene)},
                                   ctx, label='布景参考图', id_length=16)
                payload.update(scene_id=scene_id, name=options['name'], slogan=options['slogan'])
            else:
                payload.update(count=options['count'], aspect=options['aspect'], styles=[options['style']],
                               host_side=options['host_side'], stick=False)
        response = _post(session, f'{base}/api/generate', ctx, json=payload)
        identity = response.get('job_id')
        if not valid_background_id(identity):
            raise SubmissionUnknown('直播布景服务未返回有效任务编号，请核对原工作台的历史记录')
        ctx.update(remote_job_id=identity, submission_intent=False, progress='已提交直播布景')
        return identity

    def _collect(self, session, base, identity, remote, options, ctx):
        mode = options['scene_mode']
        status = remote['status']
        artifacts = []
        prefix = f'/out/jobs/{identity}/'
        ctx.update(progress='正在保存直播布景素材')

        def save(url, label, group, reuse_role=None, scene_group=None):
            if not isinstance(url, str):
                return
            path = url.partition('?')[0]
            if path.startswith(prefix) and re.fullmatch(r'[A-Za-z0-9_-]+\.png', path[len(prefix):]):
                filename = path[len(prefix):]
            elif mode != 'reference' and group in ('foreground', 'background') and re.fullmatch(
                    r'/fg-split/[a-f0-9]{16}_(?:bg|fg)\.png', path):
                filename = path.rsplit('/', 1)[-1]
            else:
                raise AdapterError('直播布景服务返回了无效的图片路径')
            image = _save_image(session, base + path, ctx, f'{group}-{filename}', label)
            if scene_group:
                image.update(scene_group=str(scene_group), scene_layer=group)
            artifacts.append({**image, 'reuse_role': reuse_role})
            ctx.update(artifacts=list(artifacts))

        results = [row for row in (remote.get('results') or [])
                   if isinstance(row, dict) and isinstance(row.get('url'), str) and row['url']]
        for index, row in enumerate(results, start=1):
            label = '直播间整图' if mode == 'reference' else f'直播间整图 {index}'
            save(row['url'], label, 'scene', 'scene')
        if status in ('error', 'cancelled') or not results:
            for index, row in enumerate(remote.get('attempts') or [], start=1):
                if isinstance(row, dict):
                    save(row.get('url'), f'待复核候选图 {index}', 'candidate')
            reason = '任务已在原工作台取消' if status == 'cancelled' else '直播布景未生成可用整图，请查看原工作台'
            raise AdapterError(reason)

        def complete_pair(row):
            return isinstance(row, dict) and row.get('bg') and row.get('fg')

        reference_pair = None
        layers_by_name = {}
        note = ''
        if mode == 'reference':
            reference_pair = next((row for row in (remote.get('layers') or []) if complete_pair(row)), None)
        else:
            try:
                groups = _get(session, f'{base}/api/assets/{identity}', ctx)
                layers_by_name = {row['name']: row for row in (groups.get('layers') or [])
                                  if complete_pair(row) and isinstance(row.get('name'), str)}
            except AdapterError:
                note = '分层暂不可读取，可在原工作台查看'
        saved_pairs = 0
        for index, row in enumerate(results, start=1):
            name = row['url'].partition('?')[0].rsplit('/', 1)[-1]
            pair = reference_pair if mode == 'reference' else layers_by_name.get(name)
            if not pair:
                continue
            suffix = '' if len(results) == 1 else f' {index}'
            save(pair['bg'], '干净背景' + suffix, 'background', scene_group=index)
            save(pair['fg'], '透明前景' + suffix, 'foreground', scene_group=index)
            saved_pairs += 1
        if not note and (status == 'partial' or saved_pairs < len(results)):
            note = '已保存整图，缺少配套分层，可在原工作台拆分'
        if len(results) < options['count']:
            note = f'已保存 {len(results)}/{options["count"]} 张整图。{note}'
        quality = 'needs_review' if note or status == 'partial' else 'passed' if mode == 'reference' else 'not_checked'
        return {'artifacts': artifacts, 'quality_status': quality, 'details': {'quality_note': note}}
