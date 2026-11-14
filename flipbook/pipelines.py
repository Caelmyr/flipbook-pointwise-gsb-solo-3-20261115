from copy import deepcopy
import math
import numpy as np
from PIL import Image, ImageDraw
from . import validate as v
from .model import find_frame, new_frame, new_layer, clone_frame
from .composite import render_frame, blend_pixels
from .pixels import apply_effect
from .drawing import font


def effect_pipeline(store, project_id, revision, body):
    steps = v.array(body.get('steps'), '效果步骤', 12)
    if not steps:
        raise v.Invalid('至少需要一个效果步骤')
    with store.edit(project_id, revision, '批量像素效果链') as project:
        ids = v.frame_ids(body.get('frame_ids'), project)
        from .layer_nodes import walk_layers
        for frame_id in ids:
            frame = find_frame(project, frame_id)
            for layer in walk_layers(frame['layers']):
                if layer.get('kind') == 'group':
                    continue
                if layer['locked']:
                    raise v.Invalid('效果链包含锁定图层')
                from .role_tools import require_ordinary
                require_ordinary(layer)
                from .cels import require_detached
                require_detached(layer)
                image = store.image(project_id, layer['asset'])
                for step in steps:
                    image = apply_effect(image, v.object_value(step, '效果步骤'))
                layer['asset'] = store.put_image(project_id, image)
    return store.get(project_id)


def motion_trail(store, project_id, revision, body):
    depth = v.integer(body.get('depth', 3), '残影帧数', 1, 12)
    opacity = v.real(body.get('opacity', .35), '残影不透明度', .01, .9)
    dx = v.integer(body.get('dx', 0), '残影横向位移', -256, 256)
    dy = v.integer(body.get('dy', 0), '残影纵向位移', -256, 256)
    with store.edit(project_id, revision, '生成运动残影') as project:
        ids = v.frame_ids(body.get('frame_ids'), project)
        original = deepcopy(project)
        for frame_id in ids:
            frame = find_frame(project, frame_id)
            if len(frame['layers']) >= 16:
                raise v.Invalid('残影图层超过限制')
            index = next(i for i, f in enumerate(original['frames']) if f['id'] == frame_id)
            trail = Image.new('RGBA', (project['width'], project['height']))
            for back in range(depth, 0, -1):
                source_index = index - back
                if source_index < 0:
                    continue
                image = render_frame(store, original, original['frames'][source_index]['id'])
                shifted = Image.new('RGBA', image.size)
                shifted.alpha_composite(image, (dx * back, dy * back))
                trail = blend_pixels(trail, shifted, 'normal', opacity ** back)
            frame['layers'].insert(0, new_layer('运动残影', store.put_image(project_id, trail)))
    return store.get(project_id)


def typewriter(store, project_id, revision, body):
    message = v.text(body.get('text'), '字幕', 80, False)
    color = v.rgba(body.get('color', '#ffffffff'))
    size = v.integer(body.get('font_size', 12), '字号', 6, 128)
    duration = v.duration(body.get('duration', 100))
    x = v.integer(body.get('x', 4), '文字X', -100, 2048)
    y = v.integer(body.get('y', 4), '文字Y', -100, 2048)
    from .caption_font import caption_font
    font_path, typeface = caption_font(body, message, size)
    count = len(message)
    with store.edit(project_id, revision, '打字机文字动画') as project:
        frame = find_frame(project, body.get('frame_id'))
        if len(project['frames']) + count > 300:
            raise v.Invalid('字幕展开超过300帧')
        if len(frame['layers']) >= 16:
            raise v.Invalid('字幕图层超过16层')
        v.pixel_budget(project['width'], project['height'],
                       sum(len(f['layers']) for f in project['frames']) + count * (len(frame['layers']) + 1))
        additions = []
        for index in range(1, count + 1):
            image = Image.new('RGBA', (project['width'], project['height']))
            ImageDraw.Draw(image).text((x, y), message[:index], font=typeface, fill=color)
            copied = clone_frame(frame)
            copied['name'] = f'字幕 {index}/{count}'
            copied['duration'] = duration
            copied['layers'].append(new_layer('打字字幕', store.put_image(project_id, image)))
            additions.append(copied)
        insertion = project['frames'].index(frame) + 1
        project['frames'][insertion:insertion] = additions
    return store.get(project_id)



def radial_burst(store, project_id, revision, body):
    count = v.integer(body.get('count', 12), '特效帧数', 4, 60)
    rays = v.integer(body.get('rays', 8), '射线数量', 3, 32)
    color = v.rgba(body.get('color', '#f8cc65ff'))
    maximum_radius = v.real(body.get('radius', 24), '扩散半径', 1, 1024)
    duration = v.duration(body.get('duration', 60))
    with store.edit(project_id, revision, '放射爆发动画') as project:
        frame = find_frame(project, body.get('frame_id'))
        if len(project['frames']) + count > 300 or len(frame['layers']) >= 16:
            raise v.Invalid('爆发动画超过帧或图层限制')
        center = v.point(body.get('center', [project['width'] / 2, project['height'] / 2]), project['width'], project['height'])
        additions = []
        for index in range(count):
            t = index / (count - 1)
            image = Image.new('RGBA', (project['width'], project['height']))
            painter = ImageDraw.Draw(image)
            radius = maximum_radius * (.1 + .9 * t)
            inner = radius * max(.2, t)
            alpha = int(color[3] * (1 - t) ** .7)
            for ray in range(rays):
                angle = ray / rays * 2 * math.pi
                first = (center[0] + math.cos(angle) * inner, center[1] + math.sin(angle) * inner)
                last = (center[0] + math.cos(angle) * radius, center[1] + math.sin(angle) * radius)
                painter.line((first, last), fill=(*color[:3], alpha), width=max(1, int(4 * (1 - t))))
            copied = clone_frame(frame)
            copied['name'] = f'爆发 {index + 1}'
            copied['duration'] = duration
            copied['layers'].append(new_layer('放射特效', store.put_image(project_id, image)))
            additions.append(copied)
        insertion = project['frames'].index(frame) + 1
        project['frames'][insertion:insertion] = additions
    return store.get(project_id)


def temporal_average(store, project_id, revision, body):
    radius = v.integer(body.get('radius', 1), '时间窗口半径', 1, 5)
    with store.edit(project_id, revision, '帧间透明平均') as project:
        ids = v.frame_ids(body.get('frame_ids'), project)
        original = deepcopy(project)
        for frame_id in ids:
            frame = find_frame(project, frame_id)
            from .layer_nodes import walk_layers
            if any(layer['locked'] for layer in walk_layers(frame['layers'])):
                raise v.Invalid('帧间平均会合并图层，请先解锁')
            index = original['frames'].index(find_frame(original, frame_id))
            subset = original['frames'][max(0, index - radius):min(len(original['frames']), index + radius + 1)]
            premultiplied = np.zeros((project['height'], project['width'], 3), dtype=float)
            alpha_total = np.zeros((project['height'], project['width'], 1), dtype=float)
            for source in subset:
                raster = np.asarray(render_frame(store, original, source['id']), dtype=float) / 255
                alpha = raster[:, :, 3:4]
                premultiplied += raster[:, :, :3] * alpha
                alpha_total += alpha
            rgb = np.divide(premultiplied, alpha_total, out=np.zeros_like(premultiplied), where=alpha_total > 1e-9)
            alpha = alpha_total / len(subset)
            image = Image.fromarray(np.rint(np.clip(np.concatenate([rgb, alpha], axis=2), 0, 1) * 255).astype(np.uint8))
            frame['layers'] = [new_layer('时间平均合成层', store.put_image(project_id, image))]
    return store.get(project_id)
