"""颜色与法线成对编辑的照明工作区。

法线图解码为图像空间（x 向右、y 向下、z 朝向观察者）的单位向量；
旋转、镜像与非均匀缩放按像素几何变换的逆转置矩阵重定向法线，
使法线跟随真实表面方向，而不是只搬动像素。
缺失或尺寸不符的配对会明确报错，透明像素不接收光照。
"""
import io
import json
import math
import zipfile
from copy import deepcopy
import numpy as np
from PIL import Image, ImageOps
from . import validate as v
from .composite import render_frame, png_bytes
from .model import find_frame


CONVENTIONS = ['opengl', 'directx']
IDENTITY = {'rotation': 0, 'flip_x': False, 'flip_y': False, 'scale_x': 1, 'scale_y': 1}
FLAT = np.array([0.0, 0.0, 1.0])


def default_state():
    return {
        'convention': 'opengl',
        'light': {'x': -0.35, 'y': -0.5, 'z': 0.79},
        'ambient': 0.3,
        'intensity': 1.0,
        'light_color': '#ffffffff',
        'transform': dict(IDENTITY),
        'shared_normal': None,
        'pairs': {},
        'confirmed': {},
    }


def state(project):
    return project.setdefault('lighting', default_state())


def current_settings(lighting):
    return {key: deepcopy(lighting[key]) for key in
            ['convention', 'light', 'ambient', 'intensity', 'light_color', 'transform']}


def valid_light(body):
    body = v.object_value(body, '光源方向')
    return {
        'x': v.real(body.get('x', 0), '光源X', -1, 1),
        'y': v.real(body.get('y', 0), '光源Y', -1, 1),
        'z': v.real(body.get('z', 1), '光源高度', 0, 1),
    }


def valid_transform(body):
    body = v.object_value(body, '表面变换')
    return {
        'rotation': v.real(body.get('rotation', 0), '旋转角度', -360, 360),
        'flip_x': v.boolean(body.get('flip_x', False), '水平镜像'),
        'flip_y': v.boolean(body.get('flip_y', False), '垂直镜像'),
        'scale_x': v.real(body.get('scale_x', 1), '横向缩放', 0.01, 8),
        'scale_y': v.real(body.get('scale_y', 1), '纵向缩放', 0.01, 8),
    }


def merge_settings(lighting, override):
    merged = current_settings(lighting)
    if override is None:
        return merged
    override = v.object_value(override, '照明设置')
    if 'convention' in override:
        merged['convention'] = v.choice(override['convention'], '法线方向约定', CONVENTIONS)
    if 'light' in override:
        merged['light'] = valid_light(override['light'])
    if 'ambient' in override:
        merged['ambient'] = v.real(override['ambient'], '环境光', 0, 1)
    if 'intensity' in override:
        merged['intensity'] = v.real(override['intensity'], '光源强度', 0, 8)
    if 'light_color' in override:
        merged['light_color'] = v.hex_color(override['light_color'])
    if 'transform' in override:
        merged['transform'] = valid_transform(override['transform'])
    return merged


def validate_settings_dict(settings):
    merge_settings(default_state(), v.object_value(settings, '照明设置'))


def validate_state(project):
    lighting = project.get('lighting')
    if lighting is None:
        return
    v.object_value(lighting, '照明工作区')
    validate_settings_dict(lighting)
    if lighting.get('shared_normal') is not None:
        v.digest(lighting['shared_normal'])
    frame_ids = {frame['id'] for frame in project['frames']}
    pairs = v.object_value(lighting.get('pairs', {}), '法线配对')
    if len(pairs) > 300:
        raise v.Invalid('法线配对数量超限')
    for frame_id, asset in pairs.items():
        if frame_id not in frame_ids:
            raise v.Invalid('法线配对引用不存在的帧')
        v.digest(asset)
    confirmed = v.object_value(lighting.get('confirmed', {}), '已确认照明')
    if len(confirmed) > 300:
        raise v.Invalid('已确认照明结果数量超限')
    for frame_id, entry in confirmed.items():
        v.key(frame_id)
        v.object_value(entry, '已确认照明')
        v.text(entry.get('frame_name'), '帧名称', 100)
        size = v.array(entry.get('size'), '确认尺寸', 2)
        if len(size) != 2:
            raise v.Invalid('确认结果缺少尺寸')
        for component in size:
            v.integer(component, '确认尺寸', 8, 2048)
        for field in ['color_asset', 'normal_asset', 'lit_asset']:
            v.digest(entry.get(field))
        validate_settings_dict(entry.get('settings', {}))


def normalize(vectors):
    length = np.linalg.norm(vectors, axis=2, keepdims=True)
    unit = np.zeros_like(vectors)
    np.divide(vectors, length, out=unit, where=length > 1e-9)
    return np.where(length > 1e-9, unit, np.broadcast_to(FLAT, vectors.shape))


def decode_normals(image, convention):
    array = np.asarray(image.convert('RGBA'), dtype=np.float64)[:, :, :3] / 255.0
    vectors = array * 2.0 - 1.0
    if convention == 'opengl':
        vectors[:, :, 1] = -vectors[:, :, 1]
    return normalize(vectors)


def encode_normals(vectors, convention):
    vectors = normalize(vectors)
    if convention == 'opengl':
        vectors = vectors * np.array([1.0, -1.0, 1.0])
    rgb = np.clip(np.rint((vectors + 1.0) * 127.5), 0, 255).astype(np.uint8)
    alpha = np.full(rgb.shape[:2] + (1,), 255, dtype=np.uint8)
    return Image.fromarray(np.concatenate([rgb, alpha], axis=2))


def normal_matrix(transform):
    """像素几何变换（镜像→缩放→PIL旋转）的线性部分之逆转置，用于重定向法线。"""
    fx = -1.0 if transform['flip_x'] else 1.0
    fy = -1.0 if transform['flip_y'] else 1.0
    angle = math.radians(transform['rotation'])
    cos, sin = math.cos(angle), math.sin(angle)
    flip = np.array([[fx, 0.0], [0.0, fy]])
    scale = np.array([[transform['scale_x'], 0.0], [0.0, transform['scale_y']]])
    rotate = np.array([[cos, -sin], [sin, cos]])
    return np.linalg.inv(rotate @ scale @ flip).T


def geometric_transform(image, transform, canvas_size):
    if transform['flip_x']:
        image = ImageOps.mirror(image)
    if transform['flip_y']:
        image = ImageOps.flip(image)
    width = max(1, int(round(image.width * transform['scale_x'])))
    height = max(1, int(round(image.height * transform['scale_y'])))
    if width * height > 16777216:
        raise v.Invalid('变换后的尺寸过大')
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.NEAREST)
    if transform['rotation']:
        image = image.rotate(-transform['rotation'], resample=Image.Resampling.NEAREST, expand=True)
    result = Image.new('RGBA', canvas_size)
    result.paste(image, ((canvas_size[0] - image.width) // 2, (canvas_size[1] - image.height) // 2))
    return result


def transform_pair(color, normal, transform, convention):
    """颜色按像素几何变换；法线先按逆转置矩阵重定向向量，再做同样的几何变换。"""
    if transform == IDENTITY:
        return color, normal
    vectors = decode_normals(normal, convention)
    matrix = normal_matrix(transform)
    xy = vectors[:, :, :2] @ matrix.T
    reoriented = np.concatenate([xy, vectors[:, :, 2:3]], axis=2)
    size = color.size
    color_out = geometric_transform(color, transform, size)
    normal_out = geometric_transform(encode_normals(reoriented, convention), transform, size)
    array = np.asarray(normal_out).copy()
    array[array[:, :, 3] == 0] = (128, 128, 255, 255)
    return color_out, Image.fromarray(array)


def shade(color, normal, settings):
    if color.size != normal.size:
        raise v.Invalid(f'颜色与法线尺寸不符：{color.width}×{color.height} 对 {normal.width}×{normal.height}')
    convention = v.choice(settings.get('convention', 'opengl'), '法线方向约定', CONVENTIONS)
    vectors = decode_normals(normal, convention)
    light = valid_light(settings.get('light', {}))
    direction = np.array([light['x'], light['y'], light['z']], dtype=np.float64)
    length = float(np.linalg.norm(direction))
    if length < 1e-6:
        raise v.Invalid('光源方向不能是零向量')
    direction /= length
    ambient = v.real(settings.get('ambient', 0.3), '环境光', 0, 1)
    intensity = v.real(settings.get('intensity', 1), '光源强度', 0, 8)
    tint = np.array(v.rgba(settings.get('light_color', '#ffffffff'))[:3], dtype=np.float64) / 255.0
    diffuse = np.clip(vectors @ direction, 0.0, 1.0)
    factor = ambient + intensity * diffuse
    array = np.asarray(color.convert('RGBA'), dtype=np.float64) / 255.0
    alpha = array[:, :, 3]
    rgb = array[:, :, :3] * factor[:, :, None] * tint
    rgb = np.where(alpha[:, :, None] > 0, rgb, 0.0)
    out = np.concatenate([np.clip(rgb, 0.0, 1.0), alpha[:, :, None]], axis=2)
    return Image.fromarray(np.rint(out * 255.0).astype(np.uint8))


def render_lit(color, normal, settings):
    transform = settings.get('transform') or dict(IDENTITY)
    if transform != IDENTITY:
        color, normal = transform_pair(color, normal, transform, settings.get('convention', 'opengl'))
    return shade(color, normal, settings)


def normal_from_alpha(image, strength, convention):
    alpha = np.asarray(image.getchannel('A'), dtype=np.float64) / 255.0
    gy, gx = np.gradient(alpha)
    vectors = np.stack([-gx * strength, -gy * strength, np.ones_like(alpha)], axis=2)
    return encode_normals(vectors, convention)


def resolve_normal(store, project, frame_id):
    lighting = state(project)
    frame = find_frame(project, frame_id)
    asset = lighting['pairs'].get(frame_id)
    if asset is None:
        asset = lighting['shared_normal']
    if asset is None:
        raise v.Invalid(f'帧「{frame["name"]}」缺少法线配对，且未设置共用法线')
    image = store.image(project['id'], asset)
    if image.size != (project['width'], project['height']):
        raise v.Invalid(f'帧「{frame["name"]}」的法线尺寸 {image.width}×{image.height} 与画布 '
                        f'{project["width"]}×{project["height"]} 不符，请重新指定或生成')
    return image


def frame_status(store, project):
    lighting = state(project)
    size = (project['width'], project['height'])
    sizes = {}

    def normal_size(asset):
        if asset not in sizes:
            try:
                sizes[asset] = store.image(project['id'], asset).size
            except v.Invalid:
                sizes[asset] = None
        return sizes[asset]

    frames = []
    for frame in project['frames']:
        asset = lighting['pairs'].get(frame['id'])
        source = 'frame' if asset else ('shared' if lighting['shared_normal'] else 'none')
        asset = asset or lighting['shared_normal']
        entry = {'frame_id': frame['id'], 'name': frame['name'], 'source': source, 'error': None}
        if asset is None:
            entry['error'] = '缺少法线配对'
        else:
            actual = normal_size(asset)
            if actual is None:
                entry['error'] = '法线像素文件缺失'
            elif actual != size:
                entry['error'] = f'法线尺寸 {actual[0]}×{actual[1]} 与画布 {size[0]}×{size[1]} 不符'
        frames.append(entry)
    return frames


def confirmed_summary(project):
    return {frame_id: entry['confirmed_at'] for frame_id, entry in state(project)['confirmed'].items()}


def referenced_assets(project):
    lighting = project.get('lighting') or {}
    size = (project['width'], project['height'])
    result = {}
    if lighting.get('shared_normal'):
        result[lighting['shared_normal']] = size
    for asset in (lighting.get('pairs') or {}).values():
        result[asset] = size
    for entry in (lighting.get('confirmed') or {}).values():
        entry_size = tuple(entry.get('size') or size)
        for field in ['color_asset', 'normal_asset', 'lit_asset']:
            if entry.get(field):
                result[entry[field]] = entry_size
    return result


def remap_assets(project, mapping):
    lighting = project.get('lighting')
    if not lighting:
        return
    if lighting.get('shared_normal') in mapping:
        lighting['shared_normal'] = mapping[lighting['shared_normal']]
    lighting['pairs'] = {frame_id: mapping[asset] for frame_id, asset in
                         lighting.get('pairs', {}).items() if asset in mapping}
    for entry in lighting.get('confirmed', {}).values():
        for field in ['color_asset', 'normal_asset', 'lit_asset']:
            if entry.get(field) in mapping:
                entry[field] = mapping[entry[field]]


def copy_referenced(store, source_id, target_id, project):
    lighting = project.get('lighting')
    if not lighting:
        return
    mapping = {}

    def copied(asset):
        if asset not in mapping:
            mapping[asset] = store.put_image(target_id, store.image(source_id, asset))
        return mapping[asset]

    if lighting.get('shared_normal'):
        lighting['shared_normal'] = copied(lighting['shared_normal'])
    lighting['pairs'] = {frame_id: copied(asset) for frame_id, asset in lighting.get('pairs', {}).items()}
    for entry in lighting.get('confirmed', {}).values():
        for field in ['color_asset', 'normal_asset', 'lit_asset']:
            if entry.get(field):
                entry[field] = copied(entry[field])


def update_settings(store, key, revision, body):
    with store.edit(key, revision, '照明工作区设置') as project:
        lighting = state(project)
        merged = merge_settings(lighting, body)
        for field_name in ['convention', 'light', 'ambient', 'intensity', 'light_color', 'transform']:
            if field_name in body:
                lighting[field_name] = merged[field_name]
    return store.get(key)


def assign_normal(store, key, revision, stream, frame_id=None):
    if frame_id is not None:
        v.key(frame_id)
    try:
        image = Image.open(stream)
        image.load()
        image = image.convert('RGBA')
    except Exception as error:
        raise v.Invalid('法线图必须是可解码的图像文件') from error
    with store.edit(key, revision, '指定法线配对') as project:
        if image.size != (project['width'], project['height']):
            raise v.Invalid(f'法线尺寸 {image.width}×{image.height} 与画布 '
                            f'{project["width"]}×{project["height"]} 不符，未指定')
        lighting = state(project)
        asset = store.put_image(key, image)
        if frame_id is None:
            lighting['shared_normal'] = asset
        else:
            find_frame(project, frame_id)
            lighting['pairs'][frame_id] = asset
    return store.get(key)


def generate_normal(store, key, revision, body):
    strength = v.real(body.get('strength', 1.5), '法线强度', 0.05, 32)
    frame_id = body.get('frame_id')
    source_id = body.get('source_frame_id')
    with store.edit(key, revision, '从Alpha生成法线') as project:
        if frame_id is not None:
            find_frame(project, v.key(frame_id))
        source = frame_id or (v.key(source_id) if source_id else None) or project['frames'][0]['id']
        normal = normal_from_alpha(render_frame(store, project, source), strength, state(project)['convention'])
        asset = store.put_image(key, normal)
        lighting = state(project)
        if frame_id is None:
            lighting['shared_normal'] = asset
        else:
            lighting['pairs'][frame_id] = asset
    return store.get(key)


def remove_normal(store, key, revision, frame_id=None):
    with store.edit(key, revision, '移除法线配对') as project:
        lighting = state(project)
        if frame_id is None:
            lighting['shared_normal'] = None
        else:
            find_frame(project, frame_id)
            lighting['pairs'].pop(frame_id, None)
    return store.get(key)


def preview(store, project, frame_id, view, override):
    v.choice(view, '预览视图', ['before', 'after', 'normal', 'color'])
    settings = merge_settings(state(project), override)
    color = render_frame(store, project, frame_id)
    normal = resolve_normal(store, project, frame_id)
    transform = settings['transform']
    if view == 'before':
        return shade(color, normal, settings)
    if transform != IDENTITY:
        color, normal = transform_pair(color, normal, transform, settings['convention'])
    if view == 'normal':
        return normal
    if view == 'color':
        return color
    return shade(color, normal, settings)


def confirm(store, key, revision, frame_ids):
    with store.edit(key, revision, '确认逐帧照明结果') as project:
        lighting = state(project)
        ids = v.frame_ids(frame_ids, project)
        settings = current_settings(lighting)
        for frame_id in ids:
            frame = find_frame(project, frame_id)
            color = render_frame(store, project, frame_id)
            normal = resolve_normal(store, project, frame_id)
            lit = render_lit(color, normal, settings)
            lighting['confirmed'][frame_id] = {
                'frame_id': frame_id,
                'frame_name': frame['name'],
                'duration': frame['duration'],
                'size': [project['width'], project['height']],
                'color_asset': store.put_image(key, color),
                'normal_asset': store.put_image(key, normal),
                'lit_asset': store.put_image(key, lit),
                'settings': deepcopy(settings),
                'source_revision': project['revision'],
                'confirmed_at': v.timestamp(),
            }
    return store.get(key)


def verify(store, key):
    with store.lock:
        project = store.get(key)
        results = []
        for frame_id, entry in state(project)['confirmed'].items():
            try:
                color = store.image(key, entry['color_asset'])
                normal = store.image(key, entry['normal_asset'])
                expected = store.image(key, entry['lit_asset'])
                actual = render_lit(color, normal, entry['settings'])
                reproduces = actual.tobytes() == expected.tobytes()
                error = None
            except v.Invalid as invalid:
                reproduces, error = False, str(invalid)
            results.append({'frame_id': frame_id, 'frame_name': entry['frame_name'],
                            'reproduces': reproduces, 'error': error, 'confirmed_at': entry['confirmed_at']})
        return {'confirmed': len(results), 'valid': all(item['reproduces'] for item in results), 'results': results}


def export_bundle(store, key):
    with store.lock:
        project = store.get(key)
        confirmed = state(project)['confirmed']
        if not confirmed:
            raise v.Invalid('尚未确认任何照明结果，请先在照明工作区确认逐帧结果')
        order = [frame['id'] for frame in project['frames'] if frame['id'] in confirmed]
        order += [frame_id for frame_id in confirmed if frame_id not in order]
        manifest = {
            'format': 'flipbook-lighting-v1',
            'project': project['name'],
            'source_revision': project['revision'],
            'canvas': [project['width'], project['height']],
            'reproduce': 'lit[i] = shade(transform_pair(color[i], normal[i], settings.transform), settings)；'
                         '法线方向约定见每帧 settings.convention，透明像素不接收光照',
            'frames': [],
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            for index, frame_id in enumerate(order):
                entry = confirmed[frame_id]
                names = {kind: f'{kind}/{index + 1:04d}.png' for kind in ['color', 'normal', 'lit']}
                archive.writestr(names['color'], png_bytes(store.image(key, entry['color_asset'])))
                archive.writestr(names['normal'], png_bytes(store.image(key, entry['normal_asset'])))
                archive.writestr(names['lit'], png_bytes(store.image(key, entry['lit_asset'])))
                manifest['frames'].append({
                    'index': index + 1, 'frame_id': frame_id, 'name': entry['frame_name'],
                    'duration': entry['duration'], 'size': entry['size'],
                    'color': names['color'], 'normal': names['normal'], 'lit': names['lit'],
                    'settings': entry['settings'], 'confirmed_at': entry['confirmed_at'],
                })
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr('README.txt',
                             'Flipbook Studio lighting pairs v1.\n'
                             'color/ 为确认时的合成颜色，normal/ 为配对法线原图，lit/ 为确认的受光结果。\n'
                             '按 manifest.json 中每帧 settings 重放 transform_pair 与 shade 可逐帧重现 lit 结果；\n'
                             '变换会按真实表面方向重定向法线向量，透明像素不接收光照。\n')
        return stream.getvalue()
