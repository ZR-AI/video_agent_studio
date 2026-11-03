from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import re

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent


@dataclass
class Settings:
    data_dir: Path = ROOT / 'data'
    repo_root: Path = REPO
    host: str = '127.0.0.1'
    port: int = 8010
    product_url: str = 'http://127.0.0.1:8008'
    motion_url: str = 'http://127.0.0.1:8001'
    fl2va_url: str = ''
    ds_key: str = field(default='', repr=False)
    rh_key: str = field(default='', repr=False)
    ds_base: str = 'https://api.deepseek.com'
    ds_model: str = 'deepseek-chat'
    rh_base: str = 'https://www.runninghub.cn'
    fl2va_workflow_id: str = ''
    fl2va_video_mode: str = 'legacy'
    seedvc_workflow_id: str = ''
    poll_interval: float = 5
    http_timeout: float = 120
    max_workers: int = 2
    max_upload_bytes: int = 200 * 1024 * 1024
    planner_mode: str = 'auto'

    @classmethod
    def load(cls) -> 'Settings':
        # Read credentials in-process; they never enter graph state or responses.
        local = dotenv_values(ROOT / '.env')
        source = os.environ.get('VIDEO_AGENT_KEY_SOURCE_FILE', local.get('KEY_SOURCE_FILE', '../fl2va/web/.dev.vars'))
        source_path = Path(source or '').expanduser()
        if not source_path.is_absolute():
            source_path = ROOT / source_path
        shared = dotenv_values(source_path) if source and source_path.is_file() else {}
        values = {**shared, **local, **os.environ}
        # Cloudflare's non-secret workflow configuration is also usable without
        # running its development server. Never load the driver's config.json.
        wrangler = REPO / 'fl2va/web/wrangler.toml'
        if wrangler.is_file():
            for key in ('RUNNINGHUB_VIDEO_WORKFLOW_ID', 'RH_WORKFLOW_ID'):
                match = re.search(r'^' + key + r'\s*=\s*[\"\']([^\"\']+)', wrangler.read_text(), re.M)
                if match and not values.get(key):
                    values[key] = match.group(1)
        return cls(
            data_dir=Path(values.get('VIDEO_AGENT_DATA_DIR', str(ROOT / 'data'))).expanduser().resolve(),
            host=values.get('VIDEO_AGENT_HOST', '127.0.0.1'),
            port=int(values.get('VIDEO_AGENT_PORT', '8010')),
            product_url=values.get('PRODUCT_STUDIO_URL', 'http://127.0.0.1:8008').rstrip('/'),
            motion_url=values.get('MOTION_STUDIO_URL', 'http://127.0.0.1:8001').rstrip('/'),
            fl2va_url=values.get('FL2VA_URL', '').rstrip('/'),
            ds_key=values.get('DEEPSEEK_API_KEY', ''), rh_key=values.get('RUNNINGHUB_API_KEY', ''),
            ds_base=values.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/'),
            ds_model=values.get('VIDEO_AGENT_MODEL', 'deepseek-chat'),
            fl2va_workflow_id=values.get('RUNNINGHUB_VIDEO_WORKFLOW_ID', ''),
            fl2va_video_mode=values.get('FL2VA_VIDEO_MODE', 'two_pass' if values.get('RUNNINGHUB_VIDEO_WORKFLOW_ID') else 'legacy'),
            seedvc_workflow_id=values.get('RH_WORKFLOW_ID', ''),
            planner_mode=values.get('VIDEO_AGENT_PLANNER', 'auto'),
            max_workers=max(1, min(3, int(values.get('VIDEO_AGENT_WORKERS', '2')))),
        )
