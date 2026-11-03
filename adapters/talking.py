"""Recoverable server-side FL2VA worker; no browser or original CLI execution."""
from __future__ import annotations

import copy
import hashlib
import http.client
import ipaddress
import json
import mimetypes
import socket
import subprocess
import time
import unicodedata
import uuid
from pathlib import Path
from urllib import error, parse, request

from .base import AdapterError, Cancelled, NeedsReview, SubmissionUnknown, artifact
from . import fl2va_helpers


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    """Pin the connection to an already validated public IP, preserving TLS SNI."""
    def connect(self):
        addresses = _Run.public_addresses('https://' + self.host)
        last_error = None
        for address in addresses:
            try:
                self.sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
                self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
                return
            except OSError as exc:
                if self.sock:
                    self.sock.close()
                last_error = exc
        raise last_error or OSError('No public media address')


class _PublicHTTPSHandler(request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context, check_hostname=self._check_hostname)


class TalkingAdapter:
    def execute(self, job, ctx):
        return _Run(job, ctx).execute()


class _Run:
    def __init__(self, job, ctx):
        self.job, self.ctx, self.settings = job, ctx, ctx.settings
        self.options = job.get('options') or {}
        self.details = copy.deepcopy(job.get('details') or {})
        self.steps = self.details.setdefault('steps', {})
        self.proxy = str(self.settings.fl2va_url or '').rstrip('/')
        self.opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self.ctx.workdir.mkdir(parents=True, exist_ok=True)

    def save(self, *, progress=None, **patch):
        self.details.update(patch)
        self.job = self.ctx.update(details=copy.deepcopy(self.details), **({'progress': progress} if progress else {}))

    def http(self, url, body=None, raw=None, content_type=None, headers=None):
        self.ctx.check_cancelled()
        headers = dict(headers or {})
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        if content_type:
            headers['Content-Type'] = content_type
        req = request.Request(url, data=raw, headers=headers)
        try:
            with self.opener.open(req, timeout=self.settings.http_timeout) as response:
                status, data = response.status, response.read()
        except error.HTTPError as exc:
            status, data = exc.code, exc.read()
        try:
            return status, json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return status, data

    def api(self, endpoint, body=None, raw=None, content_type=None):
        if self.proxy:
            url = self.proxy + '/api/' + endpoint
            headers = {'x-rh-api-key': self.settings.rh_key}
        else:
            url = self.settings.rh_base.rstrip('/') + endpoint
            headers = {'Authorization': 'Bearer ' + self.settings.rh_key}
        return self.http(url, body, raw, content_type, headers)

    @staticmethod
    def field(data, name, default=None):
        if not isinstance(data, dict):
            return default
        return data.get(name, (data.get('data') or {}).get(name, default))

    def step(self, name):
        return self.steps.setdefault(name, {})

    def upload(self, name, path):
        rec = self.step('upload:' + name)
        if rec.get('file_name'):
            return rec['file_name']
        self.ctx.update(progress='正在上传口播参考素材')
        boundary = 'agent-' + uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        # Use a generated basename: user filenames never become multipart headers.
        filename = 'asset' + path.suffix.lower()
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                   f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        payload += path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        route = 'upload' if self.proxy else '/openapi/v2/media/upload/binary'
        status, data = self.api(route, raw=payload, content_type='multipart/form-data; boundary=' + boundary)
        uploaded = self.field(data, 'fileName')
        if status != 200 or not uploaded:
            raise AdapterError(f'素材上传失败（HTTP {status}），可继续重试上传')
        rec.update(file_name=uploaded, state='done')
        self.save()
        return uploaded

    def paid(self, name, endpoint, body):
        rec = self.step(name)
        if rec.get('task_id'):
            return rec['task_id']
        if rec.get('intent'):
            raise SubmissionUnknown(f'{name} 的提交结果不明；请核对云端并补录 taskId 后继续，避免重复计费')
        self.ctx.check_cancelled()
        rec.update(intent=True, state='submitting')
        self.save(progress=f'正在提交口播步骤 {name}')  # Persist before a paid POST.
        try:
            status, data = self.api(endpoint, body)
        except Cancelled:
            # http() checks cancellation before it opens a connection.
            rec.update(intent=False, state='paused_before_submit')
            self.save()
            raise
        except Exception as exc:
            rec['state'] = 'submission_unknown'
            self.save()
            raise SubmissionUnknown(f'{name} 提交连接中断；已保留提交意图，不会自动再次提交') from exc
        task_id = self.field(data, 'taskId')
        if task_id:
            rec.update(task_id=str(task_id), state='submitted', intent=False)
            self.save()  # Save even if cancellation was requested while HTTP was in flight.
            return str(task_id)
        code = str(self.field(data, 'code', '') or self.field(data, 'errorCode', ''))
        if code in {'421', 'QUEUE_LIMIT'}:
            rec.update(intent=False, state='queue_limit')
            self.save()
            raise AdapterError(f'{name} 云端并发已满，稍后继续会重试这一步')
        # A proxy 502 may mean the upstream accepted a request but its response was lost.
        rec['state'] = 'submission_unknown'
        self.save()
        raise SubmissionUnknown(f'{name} 未返回 taskId（HTTP {status}）；请核对云端后补录，不自动重交')

    def wait(self, name, task_id):
        rec = self.step(name)
        if rec.get('state') == 'failed':
            raise AdapterError(f'{name} 云端任务已失败（{task_id}），需要明确新建任务，不会自动重交')
        if rec.get('state') in {'success', 'downloaded'}:
            return
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            self.ctx.check_cancelled()
            endpoint = 'query' if self.proxy else '/openapi/v2/query'
            try:
                status, data = self.api(endpoint, {'taskId': task_id})
            except (OSError, TimeoutError):
                self.ctx.sleep(self.settings.poll_interval)
                continue
            if status != 200:
                raise AdapterError(f'{name} 状态查询失败（HTTP {status}）；继续时会查询原任务')
            state = str(self.field(data, 'status', '')).upper()
            if state == 'SUCCESS':
                rec.pop('results', None)  # Provider result URLs may contain signed tokens.
                rec.update(state='success')
                self.save()
                return
            if state in {'FAILED', 'ERROR', 'CANCELLED', 'TIMEOUT'}:
                rec.update(state='failed', cloud_status=state)
                self.save()
                raise AdapterError(f'{name} 云端任务失败（{state}），taskId={task_id}')
            if state not in {'RUNNING', 'QUEUED', 'PENDING', 'CREATED'}:
                raise AdapterError(f'{name} 未返回可识别状态；已保留 taskId，继续时查询原任务')
            rec.update(state='running', cloud_status=state)
            self.save(progress=f'口播步骤 {name}：' + ('云端生成中' if state == 'RUNNING' else '云端排队中'))
            self.ctx.sleep(self.settings.poll_interval)
        raise AdapterError(f'{name} 等待超时；云端 taskId 已保存，可继续查询原任务')

    def download(self, name, task_id, path, kind):
        rec = self.step(name)
        if path.is_file() and path.stat().st_size and rec.get('state') == 'downloaded':
            return path
        self.wait(name, task_id)
        self.ctx.update(progress=f'正在下载口播步骤 {name} 的结果')
        headers = {}
        if self.proxy:
            url = self.proxy + '/api/media?' + parse.urlencode({'taskId': task_id, 'type': kind})
            headers = {'x-rh-api-key': self.settings.rh_key}
        else:
            status, data = self.api('/openapi/v2/query', {'taskId': task_id})
            if status != 200:
                raise AdapterError(f'{name} 下载前查询失败；继续时查询原 taskId')
            results = self.field(data, 'results', []) or []
            exts = {'image': ('.png', '.jpg', '.jpeg', '.webp'), 'video': ('.mp4',),
                    'audio': ('.mp3', '.wav', '.flac', '.m4a', '.ogg')}[kind]
            urls = [r.get('url') or r.get('fileUrl') for r in results if isinstance(r, dict)]
            url = next((u for u in urls if u and parse.urlsplit(u).path.lower().endswith(exts)), None)
            if not url:
                raise AdapterError(f'{name} 成功但没有 {kind} 结果；保留 taskId 供核查')
            self.validate_media_url(url)
        self.transfer(url, path, headers)
        rec.update(state='downloaded', path=str(path))
        self.save()
        return path

    @staticmethod
    def validate_media_url(url):
        _Run.public_addresses(url)

    @staticmethod
    def public_addresses(url):
        try:
            parsed = parse.urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.port not in {None, 443}):
                raise ValueError
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError
            return list(dict.fromkeys(item[4][0] for item in addresses))
        except (ValueError, OSError):
            raise AdapterError('云端媒体地址必须是公网 HTTPS 地址') from None

    def transfer(self, url, path, headers):
        tmp = path.with_suffix(path.suffix + '.part')
        try:
            opener = self.opener if self.proxy else request.build_opener(
                request.ProxyHandler({}), _NoRedirect(), _PublicHTTPSHandler())
            with opener.open(request.Request(url, headers=headers), timeout=self.settings.http_timeout) as response:
                if response.status != 200:
                    raise AdapterError('媒体下载失败，继续时复用原 taskId')
                size = 0
                with tmp.open('wb') as output:
                    while True:
                        self.ctx.check_cancelled()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        size += len(chunk)
                if not size:
                    raise AdapterError('云端媒体为空，继续时复用原 taskId')
            self.ctx.check_cancelled()
            tmp.replace(path)
        except (OSError, TimeoutError):
            raise AdapterError('媒体下载中断，继续时复用原 taskId') from None
        finally:
            tmp.unlink(missing_ok=True)

    def driver(self):
        return fl2va_helpers

    def plan(self, script, product):
        if self.details.get('segments'):
            return self.details['segments']
        if self.proxy:
            status, data = self.api('split', {'text': script, 'options': {'detectProduct': bool(product)}})
            segments = self.field(data, 'segments', [])
            if status != 200 or not isinstance(segments, list) or not segments:
                raise AdapterError(f'文案分段失败（HTTP {status}）')
        else:
            if not self.settings.ds_key:
                raise AdapterError('需要配置 DEEPSEEK_API_KEY 以生成口播分段')
            system = ('你是中文口播文案分段助手。保留原文，不改写。按语义和标点分段，每段适合4至15秒口播，'
                      '语速每秒约5.2字，每段最多70字。返回JSON对象，segments数组每项含text和product。'
                      + ('product表示该段介绍商品、卖点、用法或促单；避免连续两段展示商品。' if product
                         else 'product始终为false。'))
            status, data = self.http(self.settings.ds_base.rstrip('/') + '/chat/completions', {
                'model': self.settings.ds_model, 'temperature': 0.3,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': script}],
            }, headers={'Authorization': 'Bearer ' + self.settings.ds_key})
            try:
                content = data['choices'][0]['message']['content'].strip()
                if content.startswith('```'):
                    content = content.split('\n', 1)[1].rsplit('```', 1)[0]
                segments = json.loads(content)['segments']
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                raise AdapterError(f'DeepSeek 分段响应无效（HTTP {status}）') from exc
        clean = []
        driver = self.driver()
        for item in segments:
            text = (item.get('text', '') if isinstance(item, dict) else str(item)).strip()
            # Enforce the duration limit locally rather than trusting model estimates.
            for part in driver.enforce_segment_limit([text], 70):
                seconds = max(4, min(15, round(driver.speech_seconds(part, driver.DEFAULTS), 2)))
                flag = bool(product and isinstance(item, dict) and item.get('product'))
                clean.append({'text': part, 'seconds': seconds,
                              'product': flag and not (clean and clean[-1]['product'])})
        if not clean:
            raise AdapterError('分段结果为空')
        def normalize(text):
            return ''.join(ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith('P'))
        if normalize(script) != normalize(''.join(item['text'] for item in clean)):
            raise AdapterError('模型分段改变了原文，请重新分段；未提交视频任务')
        if product and not any(item['product'] for item in clean):
            clean[0]['product'] = True
            self.details['product_tag_warning'] = '模型未标记展示商品的分段，第一段默认展示；请在参考图确认时核对'
        self.save(segments=clean)
        return clean

    def composite(self, char_name, product_name, aspect):
        body = {'charImage': char_name, 'productImage': product_name, 'aspectRatio': aspect}
        endpoint = 'composite'
        if not self.proxy:
            prompt = ('以图1为唯一底图，人物身份、位置、大小、构图、背景、机位和光影严格保持不变。'
                      '让人物双手真实握住或托住图2商品，手指与商品有接触、遮挡及阴影，'
                      '商品顶边低于肩膀且不遮挡脸。仅调整手臂手部及商品，禁止悬空贴图。')
            nodes = [{'nodeId': '470', 'fieldName': 'image', 'fieldValue': char_name},
                     {'nodeId': '498', 'fieldName': 'image', 'fieldValue': product_name},
                     {'nodeId': '496', 'fieldName': 'prompt', 'fieldValue': prompt}]
            if aspect:
                nodes.append({'nodeId': '13', 'fieldName': 'aspect_ratio', 'fieldValue': aspect})
            endpoint = '/openapi/v2/run/ai-app/2109547465685954562'
            body = {'nodeInfoList': nodes, 'instanceType': 'default', 'usePersonalQueue': 'false'}
        task_id = self.paid('composite', endpoint, body)
        path = self.download('composite', task_id, self.ctx.workdir / 'holding.png', 'image')
        previews = [artifact(self.ctx.asset('reference'), '人物参考图', 'image'), artifact(path, '持商品参考图', 'image')]
        self.save(frame_artifacts=previews)
        if not self.details.get('frames_approved'):
            self.ctx.update(details=copy.deepcopy(self.details), artifacts=previews)
            raise NeedsReview('持商品参考图已生成，请检查人物、商品及背景后确认，随后继续生成视频')
        return self.upload('holding', path)

    def ffmpeg(self, args):
        self.ctx.check_cancelled()
        log = self.ctx.workdir / 'ffmpeg.log'
        with log.open('ab') as stream:
            try:
                proc = subprocess.Popen(['ffmpeg', '-nostdin', '-y', '-hide_banner', '-loglevel', 'error', *args],
                                        stdout=stream, stderr=stream)
            except FileNotFoundError as exc:
                raise AdapterError('未安装 ffmpeg，云端分段已保留，安装后可继续') from exc
            try:
                deadline = time.monotonic() + 1800
                while proc.poll() is None:
                    if time.monotonic() > deadline:
                        raise AdapterError('ffmpeg 处理超时，已有云端结果已保留')
                    self.ctx.sleep(0.2)
                if proc.returncode:
                    raise AdapterError('ffmpeg 处理失败，可查看任务目录的 ffmpeg.log；原始分段已保留')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

    def execute(self):
        script = str(self.options.get('script') or '').strip()
        if not script:
            raise AdapterError('缺少口播文案 script')
        if not self.settings.rh_key:
            raise AdapterError('需要配置 RUNNINGHUB_API_KEY')
        if self.options.get('frame_mode', 'original') != 'original':
            raise AdapterError('第一版仅支持 original 参考帧；背景取色还原和 paired 生图请在原工作台完成后上传')
        if self.options.get('tail_mode', 'none') not in {'none', 'off', ''} or self.options.get('transition', 'none') not in {'none', 'cut', ''}:
            raise AdapterError('第一版支持原始分段直接拼接；高级尾音加速和转场需使用原工作台')
        mode = str(self.options.get('video_mode') or self.settings.fl2va_video_mode or 'legacy')
        if mode not in {'legacy', 'two_pass', 'latent_two_pass', 'veda_two_pass'}:
            raise AdapterError('不支持的口播生成版本')
        if mode != (self.settings.fl2va_video_mode or 'legacy'):
            raise AdapterError('第一版仅支持当前服务配置的口播生成版本，请在服务配置中选择对应版本和工作流后新建任务')
        if not self.proxy and mode != 'legacy' and not str(self.settings.fl2va_workflow_id).isascii():
            raise AdapterError('所选口播版本需要配置有效的数字 RUNNINGHUB_VIDEO_WORKFLOW_ID')
        if not self.proxy and mode != 'legacy' and not str(self.settings.fl2va_workflow_id).isdecimal():
            raise AdapterError('所选口播版本需要配置有效的数字 RUNNINGHUB_VIDEO_WORKFLOW_ID')
        reference = self.ctx.asset('reference')
        product, last = self.ctx.asset('product', False), self.ctx.asset('last_frame', False)
        voice = self.ctx.asset('voice', False)
        voice_enabled = bool(self.options.get('voice_enabled', bool(voice)))
        if voice_enabled and not voice:
            raise AdapterError('启用换音色需要上传 voice 素材')
        if voice_enabled and not self.proxy and not self.settings.seedvc_workflow_id:
            raise AdapterError('启用换音色需要配置 RH_WORKFLOW_ID')
        binding = hashlib.sha256(json.dumps({'options': self.options, 'backend': self.proxy,
            'rh_base': self.settings.rh_base, 'workflow': self.settings.fl2va_workflow_id,
            'seedvc': self.settings.seedvc_workflow_id, 'assets': self.ctx.assets}, sort_keys=True).encode()).hexdigest()
        if self.details.get('input_binding') not in {None, binding}:
            raise AdapterError('任务配置或素材已变化，不能混用历史分段，请新建任务')
        self.save(progress='正在校验口播文案并规划分段', input_binding=binding,
                  limitations=['original参考帧', '串行生成', '直接拼接，无背景还原/尾音加速/转场'])
        segments = self.plan(script, product)
        image_name = self.upload('reference', reference)
        last_name = self.upload('last_frame', last) if last else image_name
        aspect = str(self.options.get('aspect') or '9:16')
        aspect_select = {'9:16': '4', '16:9': '5', '1:1': '1', '3:4': '2', '4:3': '3'}.get(aspect)
        if not aspect_select:
            raise AdapterError('口播比例支持 9:16、16:9、1:1、3:4 或 4:3')
        holding = ''
        if product:
            product_name = self.upload('product', product)
            composite_aspect = {'9:16': '9:16 (Portrait Widescreen)', '16:9': '16:9 (Widescreen)',
                                '1:1': '1:1 (Square)', '3:4': '3:4 (Portrait Standard)', '4:3': '4:3 (Standard)'}[aspect]
            holding = self.composite(image_name, product_name, composite_aspect)
        driver = self.driver()
        template_path = Path(self.settings.repo_root) / 'fl2va/web/public/prompt_template.txt'
        if not template_path.is_file():
            template_path = Path(__file__).resolve().parents[1] / 'resources/talking_prompt.txt'
        template = template_path.read_text(encoding='utf-8')
        paths = []
        carry = self.options.get('product_mode', 'carry') != 'independent'
        for i, seg in enumerate(segments):
            self.ctx.check_cancelled()
            previous = bool(carry and i and segments[i - 1]['product'] and holding)
            first = holding if previous else image_name
            final = holding if seg['product'] and holding else image_name if previous else last_name
            action = ('Keep the product held below shoulder height throughout; do not remove it.' if first == final == holding
                      else 'Bring the product into view from outside the frame, below shoulder height.' if seg['product']
                      else 'Take the product fully outside the frame; never make it vanish in view.' if previous else '')
            prompt = driver.render_prompt(template, seg['text'], seg['seconds']).replace('{product_action}', action)
            if any(marker in prompt for marker in ('{speech}', '{seconds}', '{product_action}')):
                raise AdapterError('口播模板仍含未替换占位符；请检查模板版本')
            endpoint = 'run'
            body = {'firstImage': first, 'lastImage': final, 'prompt': prompt,
                    'seconds': seg['seconds'], 'aspect': aspect_select, 'videoMode': mode}
            if not self.proxy:
                cfg = {'video_mode': mode, 'rh_aspect_select': aspect_select}
                if mode != 'legacy':
                    cfg[driver.VIDEO_WORKFLOW_CONFIG[mode]] = self.settings.fl2va_workflow_id
                nodes = driver.build_node_info(cfg, first, final, prompt, seg['seconds'])
                endpoint = ('/openapi/v2/run/ai-app/2084086089706459137' if mode == 'legacy'
                            else '/openapi/v2/run/workflow/' + self.settings.fl2va_workflow_id)
                body = {'nodeInfoList': nodes, 'instanceType': 'default', 'usePersonalQueue': 'false'}
            name = f'segment:{i + 1}'
            task_id = self.paid(name, endpoint, body)
            paths.append(self.download(name, task_id, self.ctx.workdir / f'seg_{i + 1:03}.mp4', 'video'))
        merged = self.ctx.workdir / 'merged.mp4'
        if not self.details.get('merged') or not merged.is_file():
            self.ctx.update(progress='全部口播片段已就绪，正在本地拼接')
            listing = self.ctx.workdir / 'concat.txt'
            listing.write_text(''.join("file '" + p.name + "'\n" for p in paths))
            temp = self.ctx.workdir / 'merged.part.mp4'
            try:
                self.ffmpeg(['-f', 'concat', '-safe', '0', '-i', str(listing), '-c', 'copy', str(temp)])
            except AdapterError:
                self.ffmpeg(['-f', 'concat', '-safe', '0', '-i', str(listing), '-c:v', 'libx264',
                             '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(temp)])
            temp.replace(merged)
            self.save(merged=str(merged))
        output = merged
        if voice_enabled:
            self.ctx.update(progress='正在准备整片换音色')
            source = self.ctx.workdir / 'source.wav'
            if not source.is_file():
                temp_source = self.ctx.workdir / 'source.part.wav'
                self.ffmpeg(['-i', str(merged), '-vn', '-ac', '1', '-ar', '24000', str(temp_source)])
                temp_source.replace(source)
            source_name, ref_name = self.upload('voice_source', source), self.upload('voice_reference', voice)
            endpoint, body = 'voice', {'sourceAudio': source_name, 'refAudio': ref_name}
            if not self.proxy:
                endpoint = '/openapi/v2/run/workflow/' + self.settings.seedvc_workflow_id
                body = {'nodeInfoList': [{'nodeId': node, 'fieldName': 'audio', 'fieldValue': value}
                                        for node, value in [('1', source_name), ('2', ref_name)]],
                        'instanceType': 'default', 'usePersonalQueue': 'false'}
            task_id = self.paid('voice', endpoint, body)
            audio = self.download('voice', task_id, self.ctx.workdir / 'converted.mp3', 'audio')
            output = self.ctx.workdir / 'final.mp4'
            temp = self.ctx.workdir / 'final.part.mp4'
            self.ffmpeg(['-i', str(merged), '-i', str(audio), '-map', '0:v:0', '-map', '1:a:0',
                         '-c:v', 'copy', '-c:a', 'aac', '-shortest', str(temp)])
            temp.replace(output)
        return {'artifacts': [artifact(output, '口播成片'), artifact(merged, '原音色成片')] if voice_enabled
                else [artifact(output, '口播成片')], 'details': self.details, 'quality_status': 'not_requested'}
