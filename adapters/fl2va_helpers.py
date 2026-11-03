"""Pure FL2VA helpers bundled for clones without the original source tree.

Extracted from fl2va/driver/rh_fl2va_driver.py. No configuration loading,
credentials, command-line runner or network calls are included.
"""
from __future__ import annotations

import re

DEFAULTS = {'speech_chars_per_second': 5.2, 'speech_pause_seconds': 0.2}
VIDEO_WORKFLOW_CONFIG = {
    'two_pass': 'runninghub_video_workflow_id',
    'veda_two_pass': 'runninghub_veda_video_workflow_id',
    'latent_two_pass': 'runninghub_latent_video_workflow_id',
}


def enforce_segment_limit(segments, max_chars):
    out = []
    for segment in segments:
        segment = (segment or '').strip()
        while len(segment) > max_chars:
            cuts = [m.end() for m in re.finditer(r'[。！？；，,!?;、]', segment[:max_chars])]
            cut = cuts[-1] if cuts else max_chars
            out.append(segment[:cut].strip())
            segment = segment[cut:].strip()
        if segment:
            out.append(segment)
    return out


def speech_seconds(text, cfg):
    characters = len(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]', text))
    pauses = len(re.findall(r'[，。！？；：、,.!?;:]', text))
    return characters / float(cfg['speech_chars_per_second']) + pauses * float(cfg.get('speech_pause_seconds', 0.2))


def render_prompt(template, speech, seconds):
    if '{speech}' not in template or '{seconds}' not in template:
        raise ValueError('口播模板需要包含 {speech} 与 {seconds} 占位符')
    return template.replace('{speech}', speech).replace('{seconds}', str(seconds))


def build_node_info(cfg, first_name, last_name, prompt_text, secs):
    mode = cfg.get('video_mode') or ('two_pass' if cfg.get('runninghub_video_workflow_id') else 'legacy')
    if mode not in {'legacy', *VIDEO_WORKFLOW_CONFIG}:
        raise ValueError('不支持的口播生成版本')
    private_video = mode != 'legacy'
    if private_video:
        workflow_id = str(cfg.get(VIDEO_WORKFLOW_CONFIG[mode]) or '').strip()
        if not workflow_id.isascii() or not workflow_id.isdecimal():
            raise ValueError('口播工作流需要有效的数字 ID')
    first_node, last_node = ('4', '37') if private_video else ('6', '4')
    duration_node, duration_field = ('6', 'value') if private_video else ('7', 'duration_seconds')
    prompt_node, prompt_field = ('7', 'value') if private_video else ('8', 'prompt')
    nodes = [
        {'nodeId': first_node, 'fieldName': 'image', 'fieldValue': first_name, 'description': '上传首帧'},
        {'nodeId': last_node, 'fieldName': 'image', 'fieldValue': last_name, 'description': '上传尾帧'},
        {'nodeId': duration_node, 'fieldName': duration_field, 'fieldValue': str(secs), 'description': '时长（秒）'},
        {'nodeId': prompt_node, 'fieldName': prompt_field, 'fieldValue': prompt_text, 'description': '输入文本'},
    ]
    aspect = str(cfg.get('rh_aspect_select') or '').strip()
    if aspect:
        nodes.insert(3, {'nodeId': '24', 'fieldName': 'select', 'fieldValue': aspect, 'description': '设置比例'})
    return nodes
