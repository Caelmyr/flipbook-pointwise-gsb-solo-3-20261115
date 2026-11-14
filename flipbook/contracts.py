from .layer_nodes import walk_layers
from . import validate as v


def validate_document(project):
    frame_ids = {frame['id'] for frame in project['frames']}
    v.text(project['name'], '项目名称', 120, False)
    v.text(project.get('notes', ''), '项目说明', 5000)
    v.rgba(project.get('background', '#00000000'))
    v.palette(project.get('palette', []))
    v.integer(project.get('loop', 0), '循环次数', 0, 65535)
    v.boolean(project.get('archived', False), '归档')
    for tag in v.array(project.get('tags', []), '标签', 20):
        v.text(tag, '标签', 30, False)
    for frame in project['frames']:
        v.text(frame['name'], '帧名称', 100, False)
        v.text(frame.get('notes', ''), '帧说明', 2000)
        for box in v.array(frame.get('boxes', []), '碰撞框', 30):
            v.rect(box, project['width'], project['height'])
            v.text(box.get('name', ''), '碰撞框名称', 60)
        for layer in walk_layers(frame['layers']):
            v.text(layer['name'], '图层名称', 100, False)
            if not isinstance(layer.get('transform'), dict):
                raise v.Invalid('图层缺少变换')
            if set(layer['transform']) != set(v.transform({})):
                raise v.Invalid('图层变换字段不完整')
    clip_ids = set()
    for clip in v.array(project.get('clips', []), '动画片段', 100):
        clip_id = v.key(clip.get('id'))
        if clip_id in clip_ids:
            raise v.Invalid('片段编号重复')
        clip_ids.add(clip_id)
        v.text(clip['name'], '片段名称', 100, False)
        ids = v.array(clip['frame_ids'], '片段帧', 300)
        if not ids or len(ids) != len(set(ids)):
            raise v.Invalid('片段必须引用非空、不重复的帧序列')
        if any(key not in frame_ids for key in ids):
            raise v.Invalid('片段引用不存在的帧')
        if 'exposures' in clip:
            exposures = v.array(clip['exposures'], '片段曝光', 300)
            if not exposures:
                raise v.Invalid('片段曝光不能为空')
            for exposure in exposures:
                if exposure.get('frame_id') not in ids:
                    raise v.Invalid('片段曝光引用不存在的帧')
                v.duration(exposure.get('duration'))
            if set(item['frame_id'] for item in exposures) != set(ids):
                raise v.Invalid('片段曝光与帧索引不一致')
        v.choice(clip.get('direction'), '片段方向', ['forward', 'reverse', 'pingpong'])
        v.integer(clip.get('repeat'), '片段循环', 0, 65535)
    map_ids = set()
    for tilemap in v.array(project.get('tilemaps', []), '地图', 20):
        map_id = v.key(tilemap.get('id'))
        if map_id in map_ids:
            raise v.Invalid('地图编号重复')
        map_ids.add(map_id)
        v.text(tilemap['name'], '地图名称', 100, False)
        from .map_animation import validate_map
        validate_map(project, tilemap)
        columns = v.integer(tilemap['columns'], '地图列数', 1, 100)
        rows = v.integer(tilemap['rows'], '地图行数', 1, 100)
        tw = v.integer(tilemap['tile_width'], '瓦片宽度', 8, 256)
        th = v.integer(tilemap['tile_height'], '瓦片高度', 8, 256)
        if columns * rows * tw * th > 16000000:
            raise v.Invalid('地图像素预算超限')
        tiles = v.array(tilemap['tiles'], '瓦片集合', 256)
        if not tiles or any(key not in frame_ids for key in tiles):
            raise v.Invalid('地图引用缺失瓦片帧，请先移除地图或调整瓦片')
        layer_ids = set()
        layers = v.array(tilemap['layers'], '地图图层', 8)
        if not layers:
            raise v.Invalid('地图至少一层')
        for layer in layers:
            layer_id = v.key(layer['id'])
            if layer_id in layer_ids:
                raise v.Invalid('地图图层编号重复')
            layer_ids.add(layer_id)
            v.text(layer['name'], '地图图层名称', 100, False)
            v.boolean(layer['visible'], '地图图层显示')
            data = v.array(layer['data'], '瓦片数据', 10000)
            if len(data) != columns * rows:
                raise v.Invalid('瓦片数据数量与地图尺寸不符')
            for raw in data:
                raw = v.integer(raw, '瓦片值', 0, 0xFFFFFFFF)
                if raw & 0x20000000:
                    raise v.Invalid('当前不支持对角翻转标记')
                if raw & 0x1FFFFFFF > len(tiles):
                    raise v.Invalid('瓦片编号超出集合')
    return project


def check_assets(store, project):
    missing = []
    wrong_size = []
    checked = set()
    assets = {asset for frame in project['frames'] for layer in walk_layers(frame['layers'])
              for asset in [layer['asset'], layer.get('role_legacy_asset')] if asset}
    from .atlas_workspaces import asset_sizes
    page_sizes = asset_sizes(project)
    assets.update(page_sizes)
    for asset in assets:
        checked.add(asset)
        try:
            image = store.image(project['id'], asset)
            if image.size != page_sizes.get(asset, (project['width'], project['height'])):
                wrong_size.append(asset)
        except (OSError, ValueError):
            missing.append(asset)
    return {'checked': len(checked), 'missing': missing, 'wrong_size': wrong_size, 'valid': not missing and not wrong_size}


def export_preflight(project, body):
    from .exporting import sequence_ids
    ids = sequence_ids(project, body)
    scale = v.integer(body.get('scale', 1), '输出倍率', 1, 8)
    count = len(ids)
    width = project['width'] * scale
    height = project['height'] * scale
    total_pixels = width * height * count
    warnings = []
    if total_pixels > 60000000:
        warnings.append('动画输出超过6000万像素预算，请缩小或减少帧')
    if body.get('format') == 'gif' and any(frame['duration'] % 10 for frame in project['frames'] if frame['id'] in ids):
        warnings.append('GIF帧时长将四舍五入至10毫秒')
    if project['width'] * project['height'] > 1000000 and body.get('format') == 'atlas':
        warnings.append('大画布建议开启透明裁边以减少图集占用')
    return {
        'frame_count': count,
        'width': width,
        'height': height,
        'total_pixels': total_pixels,
        'raw_rgba_bytes': total_pixels * 4,
        'fits_animation_budget': total_pixels <= 60000000,
        'warnings': warnings,
    }
