"""Multiplane camera: place editable layers at depths and shoot them with a keyframed camera.

Projection is a flat multiplane model.  A layer image is a plane parallel to the
camera at ``depth`` world units.  The camera sits at (x, y, z) and looks along
+depth; its focal length is in the same units as depth, so a pane at
``depth == focal`` with the camera at the origin projects 1:1.  A pane at
distance ``D = depth - z`` projects with scale ``focal / D`` and is shifted by
``(-x * scale, -y * scale)`` around the canvas centre.

The near-clip rule is explicit: a pane whose distance is less than or equal to
``near`` is omitted from the frame and reported as clipped.  Distances are
never mirrored or made absolute, so content that passes the near plane
disappears instead of flipping to the other side.

Each pane can be a bounded plane (the artboard as a physical card) or a
``cover`` pane, whose pixels repeat past its edge so a distant backdrop still
fills the frame.  Both modes are sampled with the same deterministic mapping
for preview and export.
"""
from copy import deepcopy
import io
import numpy as np
from PIL import Image
from . import validate as v
from .model import find_frame
from .composite import blend_pixels


EASING = ['linear', 'ease_in', 'ease_out', 'ease_in_out', 'smoothstep',
          'bounce', 'elastic', 'steps']
SAMPLING = ['nearest', 'bilinear']
MAX_KEYS = 100
MAX_PANES = 32
DEFAULT_DEPTH = 100.0
DEFAULT_FOCAL = 100.0


def _number(value, name, low, high):
    return v.real(value, name, low, high)


def _pane(value):
    pane = v.object_value(value, '摄影机图层')
    return {
        'layer_id': v.key(pane.get('layer_id')),
        'depth': _number(pane.get('depth', DEFAULT_DEPTH), '图层深度', .01, 5000),
        'visible': v.boolean(pane.get('visible', True), '图层参与摄影机'),
        'cover': v.boolean(pane.get('cover', False), '图层平铺覆盖画布'),
    }


def _key(value, duration):
    key = v.object_value(value, '摄影机关键点')
    return {
        'time': v.integer(key.get('time', 0), '关键点时间', 0, duration),
        'x': _number(key.get('x', 0), '摄影机X', -100000, 100000),
        'y': _number(key.get('y', 0), '摄影机Y', -100000, 100000),
        'z': _number(key.get('z', 0), '摄影机推进', -100000, 100000),
        'focal': _number(key.get('focal', DEFAULT_FOCAL), '摄影机焦距', .1, 10000),
        'easing': v.choice(key.get('easing', 'smoothstep'), '关键点缓动', EASING),
    }


def validate_rig(project, rig):
    rig = v.object_value(rig, '多平面摄影机')
    v.boolean(rig.get('enabled', True), '摄影机启用')
    duration = v.integer(rig.get('duration', 1200), '镜头时长毫秒', 50, 30000)
    v.real(rig.get('fps', 12), '镜头采样帧率', 1, 50)
    v.real(rig.get('near', 2), '近裁切距离', .01, 5000)
    v.choice(rig.get('sampling', 'nearest'), '投影采样方式', SAMPLING)
    try:
        find_frame(project, rig.get('source_frame'))
    except v.Invalid as error:
        raise v.Invalid('多平面摄影机来源帧不存在，请先调整或移除摄影机') from error
    panes = v.array(rig.get('panes', []), '摄影机图层', MAX_PANES)
    seen = set()
    for pane in panes:
        layer_id = v.key(v.object_value(pane, '摄影机图层').get('layer_id'))
        if layer_id in seen:
            raise v.Invalid('摄影机图层重复')
        seen.add(layer_id)
        _pane(pane)
    keys = v.array(rig.get('keys', []), '摄影机关键点', MAX_KEYS)
    if not keys:
        raise v.Invalid('摄影机至少需要一个位置与焦距关键点')
    previous = None
    for key in keys:
        item = _key(key, duration)
        if previous is not None and item['time'] <= previous:
            raise v.Invalid('摄影机关键点时间必须严格递增')
        previous = item['time']
    return rig


def _default_panes(frame, depth=DEFAULT_DEPTH):
    return [{'layer_id': layer['id'], 'depth': float(depth), 'visible': True, 'cover': False}
            for layer in frame.get('layers', [])]


def _pane_depths_by_name(project, rig):
    """Remember depths per layer name so a source-frame switch keeps intent."""
    mapping = {}
    try:
        frame = find_frame(project, rig.get('source_frame'))
    except v.Invalid:
        return mapping
    layers = {layer['id']: layer for layer in frame.get('layers', [])}
    for pane in rig.get('panes', []):
        layer = layers.get(pane.get('layer_id'))
        if layer is not None:
            mapping.setdefault(layer['name'], pane)
    return mapping


def _panes_for_frame(frame, previous=None):
    previous = previous or {}
    panes = []
    for layer in frame.get('layers', []):
        remembered = previous.get(layer['name'], {})
        panes.append({'layer_id': layer['id'],
                      'depth': float(remembered.get('depth', DEFAULT_DEPTH)),
                      'visible': True,
                      'cover': bool(remembered.get('cover', False))})
    return panes


def _clean_keys(keys, duration):
    result = []
    for key in keys:
        item = {
            'time': v.integer(key.get('time', 0), '关键点时间', 0, duration),
            'x': _number(key.get('x', 0), '摄影机X', -100000, 100000),
            'y': _number(key.get('y', 0), '摄影机Y', -100000, 100000),
            'z': _number(key.get('z', 0), '摄影机推进', -100000, 100000),
            'focal': _number(key.get('focal', DEFAULT_FOCAL), '摄影机焦距', .1, 10000),
            'easing': v.choice(key.get('easing', 'smoothstep'), '关键点缓动', EASING),
        }
        result.append(item)
    return result


def _normalize_keys(keys, duration):
    items = _clean_keys(v.array(keys, '摄影机关键点', MAX_KEYS), duration)
    if not items:
        raise v.Invalid('摄影机至少需要一个位置与焦距关键点')
    items.sort(key=lambda item: item['time'])
    for item in items:
        item['time'] = v.integer(item['time'], '关键点时间', 0, duration)
    if len({item['time'] for item in items}) != len(items):
        raise v.Invalid('摄影机关键点时间不能重复')
    return items


def create(store, key, revision, body):
    body = v.object_value(body, '多平面摄影机')
    with store.edit(key, revision, '建立多平面摄影机') as project:
        if project.get('camera') is not None:
            raise v.Invalid('当前项目已有摄影机，请调整现有设置或先移除')
        source = body.get('source_frame') or project['frames'][0]['id']
        frame = find_frame(project, source)
        duration = v.integer(body.get('duration', 1200), '镜头时长毫秒', 50, 30000)
        near = v.real(body.get('near', 2), '近裁切距离', .01, 5000)
        focal = v.real(body.get('focal', DEFAULT_FOCAL), '摄影机焦距', .1, 10000)
        depth = v.real(body.get('default_depth', DEFAULT_DEPTH), '默认图层深度', .01, 5000)
        panes = body.get('panes')
        panes = [_pane(item) for item in v.array(panes, '摄影机图层', MAX_PANES)] if panes is not None \
            else _default_panes(frame, depth)
        keys = body.get('keys')
        keys = _normalize_keys(keys, duration) if keys is not None else [
            {'time': 0, 'x': 0., 'y': 0., 'z': 0., 'focal': float(focal), 'easing': 'smoothstep'}]
        rig = {
            'enabled': v.boolean(body.get('enabled', True), '摄影机启用'),
            'source_frame': source,
            'duration': duration,
            'fps': v.real(body.get('fps', 12), '镜头采样帧率', 1, 50),
            'near': near,
            'sampling': v.choice(body.get('sampling', 'nearest'), '投影采样方式', SAMPLING),
            'panes': panes,
            'keys': keys,
        }
        validate_rig(project, rig)
        project['camera'] = rig
    return store.get(key)


def update(store, key, revision, body):
    body = v.object_value(body, '多平面摄影机')
    with store.edit(key, revision, '调整多平面摄影机') as project:
        rig = deepcopy(project.get('camera') or {})
        if not rig:
            raise v.Invalid('请先建立多平面摄影机')
        if 'duration' in body:
            rig['duration'] = v.integer(body['duration'], '镜头时长毫秒', 50, 30000)
        if 'fps' in body:
            rig['fps'] = v.real(body['fps'], '镜头采样帧率', 1, 50)
        if 'near' in body:
            rig['near'] = v.real(body['near'], '近裁切距离', .01, 5000)
        if 'sampling' in body:
            rig['sampling'] = v.choice(body['sampling'], '投影采样方式', SAMPLING)
        if 'enabled' in body:
            rig['enabled'] = v.boolean(body['enabled'], '摄影机启用')
        remembered = _pane_depths_by_name(project, rig)
        if 'source_frame' in body and body['source_frame'] != rig.get('source_frame'):
            frame = find_frame(project, body['source_frame'])
            rig['source_frame'] = frame['id']
            rig['panes'] = _panes_for_frame(frame, remembered)
        elif body.get('sync') is True:
            frame = find_frame(project, rig['source_frame'])
            rig['panes'] = _panes_for_frame(frame, remembered)
        if 'panes' in body:
            rig['panes'] = [_pane(item) for item in v.array(body['panes'], '摄影机图层', MAX_PANES)]
        if 'keys' in body:
            rig['keys'] = _normalize_keys(body['keys'], rig['duration'])
        elif 'duration' in body:
            existing = rig.get('keys', [])
            over = [key for key in existing if key.get('time', 0) > rig['duration']]
            if over:
                raise v.Invalid('缩短镜头后有关键点超出新时长，请同时调整关键点时间')
            rig['keys'] = _normalize_keys(existing, rig['duration'])
        validate_rig(project, rig)
        project['camera'] = rig
    return store.get(key)


def set_pane(store, key, revision, body):
    body = v.object_value(body, '摄影机图层')
    layer_id = v.key(body.get('layer_id'))
    with store.edit(key, revision, '设置图层深度') as project:
        rig = deepcopy(project.get('camera') or {})
        if not rig:
            raise v.Invalid('请先建立多平面摄影机')
        frame = find_frame(project, rig.get('source_frame'))
        if not any(layer['id'] == layer_id for layer in frame.get('layers', [])):
            raise v.Invalid('图层不属于摄影机来源帧')
        existing = next((pane for pane in rig.get('panes', []) if pane['layer_id'] == layer_id), None)
        pane = {
            'layer_id': layer_id,
            'depth': _number(body.get('depth', existing['depth'] if existing else DEFAULT_DEPTH),
                             '图层深度', .01, 5000),
            'visible': v.boolean(body.get('visible', existing['visible'] if existing else True), '图层参与摄影机'),
            'cover': v.boolean(body.get('cover', existing.get('cover', False) if existing else False), '图层平铺覆盖画布'),
        }
        panes = [item for item in rig.get('panes', []) if item['layer_id'] != layer_id] + [pane]
        panes.sort(key=lambda item: item['depth'])
        rig['panes'] = panes
        validate_rig(project, rig)
        project['camera'] = rig
    return store.get(key)


def remove(store, key, revision):
    with store.edit(key, revision, '移除多平面摄影机') as project:
        if 'camera' not in project:
            raise v.Invalid('当前项目没有多平面摄影机')
        project.pop('camera')
    return store.get(key)


def pose_at(rig, time):
    from .tween import ease
    keys = rig['keys']
    time = max(0, min(rig['duration'], time))
    index = next((i for i in range(len(keys) - 1) if keys[i]['time'] <= time <= keys[i + 1]['time']), None)
    if index is None:
        key = keys[0] if time <= keys[0]['time'] else keys[-1]
        pose = {name: key[name] for name in ('x', 'y', 'z', 'focal')}
        factor = 0.
        easing = key.get('easing', 'smoothstep')
    else:
        first, second = keys[index], keys[index + 1]
        factor = (time - first['time']) / (second['time'] - first['time'])
        easing = first.get('easing', 'smoothstep')
        eased = ease(factor, easing)
        pose = {name: first[name] + (second[name] - first[name]) * eased
                for name in ('x', 'y', 'z', 'focal')}
    pose.update(time=time, factor=round(factor, 6), easing=easing)
    return pose


def evaluate(project, rig, time):
    """Resolve the pose and every pane's projection state without touching pixels."""
    if not rig.get('enabled', True):
        raise v.Invalid('多平面摄影机已停用')
    pose = pose_at(rig, time)
    frame = find_frame(project, rig['source_frame'])
    layers = {layer['id']: layer for layer in frame.get('layers', [])}
    panes = []
    for pane in rig.get('panes', []):
        layer = layers.get(pane['layer_id'])
        depth = float(pane.get('depth', DEFAULT_DEPTH))
        distance = depth - pose['z']
        entry = {
            'layer_id': pane['layer_id'],
            'name': layer['name'] if layer else '已移除图层',
            'depth': depth,
            'distance': round(distance, 6),
            'cover': bool(pane.get('cover', False)),
            'scale': None,
            'offset_x': None,
            'offset_y': None,
            'visible': False,
            'clipped': False,
            'reason': None,
        }
        if layer is None:
            entry['reason'] = 'stale'
        elif not pane.get('visible', True):
            entry['reason'] = 'pane_disabled'
        elif not layer.get('visible', True):
            entry['reason'] = 'layer_hidden'
        elif distance <= rig['near']:
            entry['reason'] = 'near_clip'
            entry['clipped'] = True
        else:
            scale = pose['focal'] / distance
            entry['scale'] = round(scale, 6)
            entry['offset_x'] = round(-pose['x'] * scale, 6)
            entry['offset_y'] = round(-pose['y'] * scale, 6)
            entry['visible'] = True
            entry['projected_size'] = [max(1, int(round(project['width'] * scale))),
                                       max(1, int(round(project['height'] * scale)))]
        panes.append(entry)
    return {'time': pose['time'], 'pose': pose, 'near': rig['near'], 'panes': panes,
            'clipped_count': sum(1 for pane in panes if pane['clipped']),
            'visible_count': sum(1 for pane in panes if pane['visible'])}


def info(store, project, rig):
    frame = find_frame(project, rig['source_frame'])
    layers = {layer['id']: layer for layer in frame.get('layers', [])}
    listed = {pane['layer_id']: pane for pane in rig.get('panes', [])}
    entries = []
    for layer in frame.get('layers', []):
        pane = listed.get(layer['id'])
        entries.append({
            'layer_id': layer['id'],
            'name': layer['name'],
            'kind': layer.get('kind', 'layer'),
            'layer_visible': layer.get('visible', True),
            'listed': pane is not None,
            'depth': pane['depth'] if pane else None,
            'pane_visible': pane.get('visible', True) if pane else False,
            'cover': pane.get('cover', False) if pane else False,
        })
    stale = [pane['layer_id'] for pane in rig.get('panes', []) if pane['layer_id'] not in layers]
    unlisted = [layer['id'] for layer in frame.get('layers', []) if layer['id'] not in listed]
    count = max(1, int(round(rig['duration'] * rig['fps'] / 1000)))
    warnings = []
    if not listed:
        warnings.append('摄影机还没有包含任何图层')
    if unlisted:
        warnings.append(f'{len(unlisted)} 个当前帧图层未加入摄影机，可“同步当前帧图层”')
    if stale:
        warnings.append(f'{len(stale)} 个已移除图层仍在摄影机设置中，同步后将清理')
    always = [pane for pane in rig.get('panes', []) if pane.get('depth', DEFAULT_DEPTH) <= rig['near']]
    if always:
        warnings.append(f'{len(always)} 个图层深度不大于近裁切距离，预览中会被裁掉')
    if count > 300:
        warnings.append('按当前时长与帧率会超过300帧，生成前请降低帧率或缩短时长')
    return {
        'camera': rig,
        'layers': entries,
        'unlisted': unlisted,
        'stale': stale,
        'preview_frames': count,
        'frame_ms': round(rig['duration'] / count, 3),
        'warnings': warnings,
        'near_rule': '图层距离 = 深度 − 摄影机推进；距离 ≤ 近裁切距离时整层按近裁切丢弃，不会镜像到另一侧。',
        'projection': '投影比例 = 焦距 ÷ 距离，画布中心为基准；预览与导出使用同一投影。',
    }


def _pane_source(store, project, layer):
    size = (project['width'], project['height'])
    if layer.get('kind') == 'group':
        from .layer_tree import render_tree
        node = deepcopy(layer)
        node['opacity'] = 1
        node['blend'] = 'normal'
        return render_tree([node], size, lambda asset: store.image(project['id'], asset))
    from .layer_tree import local_matrix, rasterize
    from .role_tools import display_image
    if layer.get('scene_source'):
        from .composition import render_external
        image = render_external(store, layer['scene_source'])
    else:
        image = display_image(store, project, layer)
    matrix = local_matrix(layer.get('transform', {}), size)
    if layer.get('affine'):
        matrix = matrix @ np.asarray(layer['affine'], dtype=float)
    return rasterize(image, matrix, size)


def _mirror(value, span):
    """Repeat with mirroring so a cover pane has no hard seam at its own edge."""
    phase = np.mod(value, 2 * span)
    return np.where(phase > span, 2 * span - phase, phase)


def _project(source, scale, offset, canvas, sampling, cover):
    """Map a pane plane onto the canvas.  Deterministic for preview and export."""
    width, height = canvas
    src = np.asarray(source.convert('RGBA'))
    sw = max(1, int(round(width * scale)))
    sh = max(1, int(round(height * scale)))
    left = (width - sw) / 2 + offset[0]
    top = (height - sh) / 2 + offset[1]
    xs = np.arange(width) + 0.5
    ys = np.arange(height) + 0.5
    u = xs[None, :] - left
    v = ys[:, None] - top
    if cover:
        u = _mirror(u, sw)
        v = _mirror(v, sh)
        valid = np.ones((height, width), dtype=bool)
    else:
        valid = (u >= 0) & (u < sw) & (v >= 0) & (v < sh) & np.ones((height, width), dtype=bool)
    ratio = 1 / scale
    fx = u * ratio
    if sampling == 'bilinear':
        px = np.clip(fx - 0.5, 0, source.width - 1)
        x0 = np.floor(px).astype(int)
        x1 = np.minimum(x0 + 1, source.width - 1)
        wx = px - x0
    else:
        x0 = np.clip(np.floor(fx).astype(int), 0, source.width - 1)
        x1 = x0
        wx = 0
    fy = v * ratio
    if sampling == 'bilinear':
        py = np.clip(fy - 0.5, 0, source.height - 1)
        y0 = np.floor(py).astype(int)
        y1 = np.minimum(y0 + 1, source.height - 1)
        wy = py - y0
    else:
        y0 = np.clip(np.floor(fy).astype(int), 0, source.height - 1)
        y1 = y0
        wy = 0
    x0, x1 = np.broadcast_to(x0, (height, width)), np.broadcast_to(x1, (height, width))
    y0, y1 = np.broadcast_to(y0, (height, width)), np.broadcast_to(y1, (height, width))
    wx = np.broadcast_to(wx, (height, width))[:, :, None]
    wy = np.broadcast_to(wy, (height, width))[:, :, None]
    top_left = src[y0, x0].astype(np.float32)
    if sampling == 'bilinear':
        top_right = src[y0, x1].astype(np.float32)
        bottom_left = src[y1, x0].astype(np.float32)
        bottom_right = src[y1, x1].astype(np.float32)
        mixed = ((top_left * (1 - wx) + top_right * wx) * (1 - wy)
                 + (bottom_left * (1 - wx) + bottom_right * wx) * wy)
    else:
        mixed = top_left
    mixed[~valid] = 0
    return Image.fromarray(np.rint(np.clip(mixed, 0, 255)).astype(np.uint8))


def render(store, project, rig, time, cache=None, report=None):
    """Render one transparent camera frame. Preview and export share this path."""
    state = evaluate(project, rig, time)
    if report is not None:
        report.update(state)
    frame = find_frame(project, rig['source_frame'])
    layers = {layer['id']: layer for layer in frame.get('layers', [])}
    output = Image.new('RGBA', (project['width'], project['height']))
    cache = {} if cache is None else cache
    for entry in sorted(state['panes'], key=lambda item: -item['depth']):
        if not entry['visible']:
            continue
        layer = layers[entry['layer_id']]
        if entry['layer_id'] not in cache:
            cache[entry['layer_id']] = _pane_source(store, project, layer)
        plane = _project(cache[entry['layer_id']], entry['scale'],
                         (entry['offset_x'], entry['offset_y']),
                         (project['width'], project['height']),
                         rig['sampling'], entry['cover'])
        output = blend_pixels(output, plane, layer.get('blend', 'normal'), layer.get('opacity', 1))
    return output


def render_times(rig):
    duration = v.integer(rig.get('duration', 1200), '镜头时长毫秒', 50, 30000)
    fps = v.real(rig.get('fps', 12), '镜头采样帧率', 1, 50)
    count = max(1, int(round(duration * fps / 1000)))
    if count > 300:
        raise v.Invalid('摄影机动画超过300帧，请降低帧率或缩短镜头')
    base, remainder = divmod(duration, count)
    if base < 10:
        raise v.Invalid('摄影机采样帧时长低于10毫秒，请降低帧率')
    times = []
    elapsed = 0
    for index in range(count):
        length = base + (1 if index < remainder else 0)
        times.append({'index': index, 'start': elapsed, 'duration': length, 'time': elapsed})
        elapsed += length
    return times, elapsed


def render_animation(store, project, container='apng'):
    rig = project.get('camera')
    if not rig:
        raise v.Invalid('请先建立多平面摄影机')
    if not rig.get('enabled', True):
        raise v.Invalid('多平面摄影机已停用')
    times, total = render_times(rig)
    cache = {}
    images = []
    clipped = 0
    for item in times:
        report = {}
        images.append(render(store, project, rig, item['time'], cache, report))
        if report.get('clipped_count'):
            clipped += 1
    stream = io.BytesIO()
    if container == 'webp':
        images[0].save(stream, format='WEBP', save_all=True, append_images=images[1:],
                       duration=[item['duration'] for item in times], loop=0, lossless=True, method=4)
    else:
        images[0].save(stream, format='PNG', save_all=True, append_images=images[1:],
                       duration=[item['duration'] for item in times], loop=0,
                       disposal=0, blend=0, optimize=False)
    details = {
        'format': 'APNG' if container != 'webp' else 'WEBP',
        'requested_frames': len(images),
        'duration': total,
        'requested_fps': rig['fps'],
        'actual_fps': round(len(images) / total * 1000, 6),
        'near': rig['near'],
        'sampling': rig['sampling'],
        'frames_with_clipped_panes': clipped,
        'transparent': True,
        'same_projection_as_preview': True,
    }
    return stream.getvalue(), details
