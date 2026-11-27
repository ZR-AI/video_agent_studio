"""Compose same-session LiveBG layers with a keyed presenter."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import json
import subprocess
from tempfile import TemporaryDirectory
import time

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from .adapters.base import AdapterError, artifact


class CompositionOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scene: str = Field(pattern=r'^[a-f0-9]{64}$')
    person: str = Field(pattern=r'^[a-f0-9]{64}$')
    color: str = Field(default='#00ff00', pattern=r'^#[a-fA-F0-9]{6}$')
    similarity: int = Field(default=24, ge=1, le=50)
    x: int = Field(default=50, ge=0, le=100)
    y: int = Field(default=86, ge=0, le=100)
    scale: int = Field(default=76, ge=15, le=160)


class CompositionPreview(CompositionOptions):
    seconds: float = Field(default=0, ge=0, allow_inf_nan=False)


@dataclass(frozen=True)
class Scene:
    label: str
    date: str
    background: Path
    foreground: Path


@dataclass(frozen=True)
class Person:
    label: str
    date: str
    kind: str
    path: Path


def _local(store, item):
    value = item.get('path')
    if not value:
        return None
    path = Path(value).resolve()
    return path if path.is_relative_to(store.root) and path.is_file() else None


def _key(*paths):
    return sha256('\0'.join(map(str, paths)).encode()).hexdigest()


def sources(store, session_id):
    """Return stable references to usable media, without exposing its local path."""
    scenes, people = {}, {}
    for job in store.list('jobs', session_id):
        if job['state'] != 'completed':
            continue
        items = job.get('artifacts') or []
        created = job.get('created_at', '')
        if job['tool'] == 'background':
            pairs = {}
            single_scene = sum(item.get('reuse_role') == 'scene' for item in items) == 1
            for item in items:
                layer = item.get('scene_layer')
                group = item.get('scene_group')
                # Earlier jobs identified corresponding layers with a shared label suffix.
                if not group:
                    label, _, index = (item.get('label') or '').partition(' ')
                    layer = {'干净背景': 'background', '透明前景': 'foreground'}.get(label)
                    group = index if index.isascii() and index.isdigit() else '1' if single_scene else None
                if group and layer in ('background', 'foreground') and (path := _local(store, item)):
                    pairs.setdefault(group, {})[layer] = path
            for group, pair in pairs.items():
                if {'background', 'foreground'} <= pair.keys():
                    bg, fg = pair['background'], pair['foreground']
                    scenes[_key(bg, fg)] = Scene(f'直播布景 {group}', created, bg, fg)
        elif job['tool'] in ('talking', 'motion', 'portrait_image', 'qwen_image'):
            kind = 'video' if job['tool'] in ('talking', 'motion') else 'image'
            for item in items:
                if item.get('kind') == kind and (path := _local(store, item)):
                    label = item.get('label', '人物图') if job['tool'] != 'motion' else '动作迁移成片'
                    people[_key(path)] = Person(label, created, kind, path)
    for asset in store.list('assets', session_id):
        if asset['role'] == 'reference' and asset.get('kind') == 'image' and (path := _local(store, asset)):
            people[_key(path)] = Person(asset['name'], asset.get('created_at', ''), 'image', path)
    return scenes, people


def resolve(store, session_id, options):
    scenes, people = sources(store, session_id)
    scene, person = scenes.get(options.scene), people.get(options.person)
    if not scene or not person:
        raise AdapterError('所选布景或人物素材已不可用，请从当前会话重新选择')
    return scene, person


def _canvas(scene):
    try:
        with Image.open(scene.background) as bg, Image.open(scene.foreground) as fg:
            if bg.size != fg.size:
                raise AdapterError('布景背景与前景尺寸不同，请重新拆分同一张整图')
            width, height = bg.size
            if not (2 <= width <= 4096 and 2 <= height <= 4096):
                raise AdapterError('布景尺寸超出合成范围')
            if 'A' not in fg.getbands() and 'transparency' not in fg.info:
                raise AdapterError('桌台前景缺少透明通道，请重新拆分布景')
            low, high = fg.convert('RGBA').getchannel('A').getextrema()
            if low > 32 or high < 170:
                raise AdapterError('桌台前景需要同时包含透明区域和桌台内容')
    except OSError:
        raise AdapterError('布景图片无法读取') from None
    return width - width % 2, height - height % 2


def _video_info(source):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
             'stream=avg_frame_rate,duration:format=duration', '-of', 'json', str(source)],
            check=True, capture_output=True, timeout=10,
        )
        payload = json.loads(result.stdout)
        stream = payload['streams'][0]
        start_rate = Fraction(stream['avg_frame_rate'])
        fps = min(max(start_rate, 1), 60)
        duration = float(stream.get('duration') or payload['format']['duration'])
        if duration <= 0:
            raise ValueError
    except (ValueError, KeyError, IndexError, OSError, ZeroDivisionError, subprocess.SubprocessError):
        raise AdapterError('人物视频无法读取或缺少有效时长') from None
    return str(fps), duration


def _run(args, log_path, *, timeout, ctx=None):
    with log_path.open('ab') as log:
        try:
            proc = subprocess.Popen(
                ['ffmpeg', '-nostdin', '-y', '-hide_banner', '-loglevel', 'error',
                 '-filter_complex_threads', '2', *args], stdout=log, stderr=log,
            )
        except FileNotFoundError:
            raise AdapterError('本机需要安装 FFmpeg 才能合成直播间') from None
        deadline = time.monotonic() + timeout
        try:
            while proc.poll() is None:
                if ctx:
                    ctx.check_cancelled()
                if time.monotonic() >= deadline:
                    raise AdapterError('直播间合成超时，素材已保留')
                try:
                    proc.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            if proc.returncode:
                raise AdapterError('FFmpeg 合成失败，请检查素材格式及任务目录的 ffmpeg.log')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _render(scene, person, options, destination, log, *, seconds=None, mask=None, ctx=None):
    width, height = _canvas(scene)
    fps = '25'
    if person.kind == 'video':
        fps, duration = _video_info(person.path)
        if seconds is not None and seconds >= duration:
            raise AdapterError(f'预览时间须小于视频时长（{duration:.2f} 秒）')
    canvas = f'{width}:{height}'
    person_width = max(2, round(width * options.scale / 200) * 2)
    x, y = round(width * options.x / 100), round(height * options.y / 100)
    red, green, blue = bytes.fromhex(options.color[1:])
    despill = ''
    if green > max(red, blue):
        despill = ',despill=type=green'
    elif blue > max(red, green):
        despill = ',despill=type=blue'
    split = '[host]'
    if mask:
        # After seeking, the first decoded frame can start just after timestamp zero.
        split = ',setpts=PTS-STARTPTS,split[host][matte];[matte]alphaextract,scale=96:96[mask]'
    graph = (
        f'[0:v]scale={canvas},setsar=1,format=rgba[room];'
        f'[1:v]format=rgba,scale={canvas},setsar=1[desk];'
        f'[2:v]format=rgba,colorkey=0x{options.color[1:]}:{options.similarity / 100}:0.06'
        f'{despill},scale={person_width}:-2,setsar=1{split};'
        f'[room][host]overlay=x={x}-w/2:y={y}-h:shortest=1:format=rgb[placed];'
        '[placed][desk]overlay=repeatlast=1:format=rgb[final]'
    )
    args = ['-loop', '1', '-framerate', fps, '-i', str(scene.background), '-i', str(scene.foreground)]
    if person.kind == 'video' and seconds:
        args += ['-ss', str(seconds)]
    args += ['-i', str(person.path), '-filter_complex', graph, '-map', '[final]']
    if mask:
        args += ['-frames:v', '1', '-update', '1', str(destination),
                 '-map', '[mask]', '-frames:v', '1', '-update', '1', str(mask)]
    else:
        args += ['-map', '2:a:0?', '-c:a', 'aac', '-b:a', '160k', '-c:v', 'libx264', '-threads', '4',
                 '-preset', 'fast', '-crf', '19', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(destination)]
    _run(args, log, timeout=25 if mask else 1800, ctx=ctx)
    if not destination.is_file() or not destination.stat().st_size or (mask and not mask.is_file()):
        raise AdapterError('没有读到所选帧，请尝试更早的预览时间')


def preview(scene, person, options):
    with TemporaryDirectory(prefix='studio-preview-') as directory:
        folder = Path(directory)
        destination, mask = folder / 'preview.png', folder / 'mask.png'
        _render(scene, person, options, destination, folder / 'ffmpeg.log', seconds=options.seconds, mask=mask)
        with Image.open(mask) as image:
            histogram = image.convert('L').histogram()
        # Entirely opaque or transparent frames usually indicate the wrong key color.
        usable = min(sum(histogram[:32]), sum(histogram[170:])) >= 96
        return destination.read_bytes(), usable


class CompositionAdapter:
    def __init__(self, store):
        self.store = store

    def execute(self, job, ctx):
        options = CompositionOptions.model_validate(job['options'])
        scene, person = resolve(self.store, job['session_id'], options)
        if person.kind != 'video':
            raise AdapterError('请选择人物口播或动作迁移的视频成片')
        ctx.check_cancelled()
        ctx.update(progress='正在抠像并叠加直播间布景')
        output = ctx.workdir / '直播间成片.mp4'
        temporary = ctx.workdir / 'composition.part.mp4'
        try:
            _render(scene, person, options, temporary, ctx.workdir / 'ffmpeg.log', ctx=ctx)
            ctx.check_cancelled()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return {'artifacts': [artifact(output, '直播间成片')]}
