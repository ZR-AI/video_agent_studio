from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import threading


class AdapterError(RuntimeError):
    """An execution error whose message is safe to show in the UI."""


class SubmissionUnknown(AdapterError):
    """A paid request may have reached the provider. Never resubmit it."""


class NeedsReview(AdapterError):
    """The original tool needs human review before proceeding."""


class Cancelled(AdapterError):
    pass


@dataclass
class ExecutionContext:
    settings: Any
    workdir: Path
    assets: dict[str, dict]
    update: Callable[..., dict]
    stop_event: threading.Event

    def asset(self, role: str, required: bool = True) -> Path | None:
        item = self.assets.get(role)
        if not item:
            if required:
                raise AdapterError(f'缺少素材：{role}')
            return None
        path = Path(item['path'])
        if not path.is_file():
            raise AdapterError(f'素材文件不存在：{role}')
        return path

    def check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise Cancelled('任务已暂停；已有云端任务将保留，继续时查询原任务')

    def sleep(self, seconds: float) -> None:
        if self.stop_event.wait(seconds):
            self.check_cancelled()


def artifact(path: Path, label: str = '成片', kind: str = 'video') -> dict:
    return {'path': str(path.resolve()), 'label': label, 'kind': kind}
