from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from .adapters.base import AdapterError, Cancelled, ExecutionContext, NeedsReview, SubmissionUnknown
from .adapters.background import BackgroundAdapter, valid_background_id
from .adapters.http_projects import ProductAdapter, MotionAdapter
from .adapters.image_projects import PortraitImageAdapter, QwenImageAdapter, valid_qwen_id
from .adapters.talking import TalkingAdapter
from .composition import CompositionAdapter


class Engine:
    def __init__(self, store, settings, adapters=None):
        self.store, self.settings = store, settings
        self.adapters = adapters or {'product': ProductAdapter(), 'talking': TalkingAdapter(), 'motion': MotionAdapter(),
                                     'qwen_image': QwenImageAdapter(), 'portrait_image': PortraitImageAdapter(),
                                     'background': BackgroundAdapter(), 'composition': CompositionAdapter(store)}
        self.pool = ThreadPoolExecutor(max_workers=settings.max_workers, thread_name_prefix='video-agent')
        self.lock = threading.RLock()
        self.active = {}
        self.closing = False

    def kick(self):
        with self.lock:
            if self.closing:
                return
            for job in self.store.list('jobs'):
                if job['state'] == 'queued' and job['id'] not in self.active:
                    stop = threading.Event()
                    self.active[job['id']] = stop
                    self.pool.submit(self._run, job['id'], stop)

    def _run(self, identity, stop):
        try:
            job = self.store.get('jobs', identity)
            if stop.is_set() or job['state'] != 'queued':
                return
            job = self.store.update('jobs', identity, state='running', progress='正在准备任务', error='')
            assets = {role: self.store.get('assets', asset_id) for role, asset_id in job['asset_ids'].items()}
            workdir = self.store.root / 'jobs' / identity
            workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            context = ExecutionContext(self.settings, workdir, assets,
                                       lambda **patch: self.store.update('jobs', identity, **patch), stop)
            result = self.adapters[job['tool']].execute(job, context)
            progress = '图片已保存' if job['tool'] in {'qwen_image', 'portrait_image', 'background'} else '成片已保存'
            self.store.update('jobs', identity, **result, state='completed', progress=progress, error='')
        except Cancelled as exc:
            self.store.update('jobs', identity, state='interrupted' if self.closing else 'paused', progress=str(exc), error='')
        except SubmissionUnknown as exc:
            self.store.update('jobs', identity, state='submission_unknown', error=str(exc), progress='提交状态需要核对')
        except NeedsReview as exc:
            self.store.update('jobs', identity, state='awaiting_review', error='', progress=str(exc))
        except AdapterError as exc:
            self.store.update('jobs', identity, state='failed', error=str(exc), progress='执行中断，已有结果已保留')
        except Exception as exc:
            # An exception may contain provider URLs or credentials. Never expose
            # repr(exc) or a raw requests response in task records.
            self.store.update('jobs', identity, state='failed', error=f'本地执行器异常（{type(exc).__name__}），已有提交记录已保留',
                              progress='执行中断，可核对记录后继续')
        finally:
            with self.lock:
                self.active.pop(identity, None)

    def pause(self, identity):
        with self.lock:
            job = self.store.get('jobs', identity)
            if job['state'] == 'completed':
                raise ValueError('任务已经完成')
            if identity in self.active:
                self.active[identity].set()
            return self.store.update('jobs', identity, state='paused', progress='已请求暂停本地处理；云端已提交任务保留')

    def resume(self, identity, *, remote_job_id=None, frames_approved=False, step_name=None, task_id=None):
        with self.lock:
            if identity in self.active:
                raise ValueError('任务仍在执行或停止中，请稍后继续')
            job = self.store.get('jobs', identity)
            if job['state'] == 'completed':
                raise ValueError('任务已经完成')
            if job['state'] not in {'failed', 'interrupted', 'paused', 'awaiting_review', 'submission_unknown'}:
                raise ValueError('当前任务不需要继续')
            details = dict(job.get('details') or {})
            changes = {}
            if remote_job_id:
                if job['tool'] in {'talking', 'portrait_image'} or job.get('remote_job_id') or not job.get('submission_intent'):
                    raise ValueError('仅可为提交状态未知且没有后端任务编号的任务补录编号')
                if job['tool'] == 'qwen_image' and not valid_qwen_id(remote_job_id):
                    raise ValueError('请输入造型工作台中的完整 UUID 任务编号')
                if job['tool'] == 'background' and not valid_background_id(remote_job_id):
                    raise ValueError('请输入直播布景原工作台的完整任务编号')
                changes['remote_job_id'] = remote_job_id
            if step_name or task_id:
                if job['tool'] != 'talking' or not step_name or not task_id:
                    raise ValueError('口播补录需要步骤名及对应的云端 taskId')
                steps = details.get('steps') or {}
                rec = steps.get(step_name)
                if not rec or not rec.get('intent') or rec.get('task_id'):
                    raise ValueError('此步骤没有待核对的提交')
                rec = {**rec, 'task_id': task_id, 'intent': False, 'state': 'submitted'}
                details['steps'] = {**steps, step_name: rec}
            if job['state'] == 'submission_unknown':
                unknown_steps = [r for r in (details.get('steps') or {}).values() if r.get('intent') and not r.get('task_id')]
                if unknown_steps and step_name and task_id:
                    return self.store.update('jobs', identity, details=details, progress='已保存此步骤的编号；请继续补录其余待核对步骤')
                if unknown_steps or (job['tool'] != 'talking' and not job.get('remote_job_id') and not changes.get('remote_job_id')):
                    raise ValueError('请先核对并补录原任务编号，不能重新提交未知任务')
            if frames_approved:
                if job['tool'] != 'talking' or job['state'] != 'awaiting_review':
                    raise ValueError('当前任务没有待确认参考帧')
                details['frames_approved'] = True
            if job.get('remote_job_id') and job['state'] in {'failed', 'interrupted', 'awaiting_review'}:
                details['resume_requested'] = True
            self.store.update('jobs', identity, **changes, details=details, state='queued', error='', progress='等待继续已有任务')
            self.kick()
            return self.store.get('jobs', identity)

    def close(self):
        with self.lock:
            self.closing = True
            for event in self.active.values():
                event.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
