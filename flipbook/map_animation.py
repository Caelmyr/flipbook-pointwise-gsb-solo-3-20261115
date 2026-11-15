from copy import deepcopy
from PIL import Image
from . import validate as v
from .model import find_frame
from .composite import render_frame


GID_MASK = 0x1FFFFFFF
FLIP_MASK = 0xE0000000


def clip_schedule(project, clip_id):
    clip = next((item for item in project['clips'] if item['id'] == clip_id), None)
    if clip is None:
        raise v.Invalid('动态瓦片引用的动作片段不存在')
    if clip['repeat'] != 0:
        raise v.Invalid('动态瓦片当前使用无限循环片段，请将该片段循环次数设为0')
    sequence = [(key, find_frame(project, key)['duration']) for key in clip['frame_ids']]
    if clip.get('exposures'):
        sequence = [(item['frame_id'], item['duration']) for item in clip['exposures']]
    if clip['direction'] == 'reverse':
        sequence.reverse()
    elif clip['direction'] == 'pingpong':
        sequence += sequence[-2:0:-1]
    return sequence


def frame_at(sequence, elapsed):
    elapsed %= sum(duration for _, duration in sequence)
    for frame, duration in sequence:
        if elapsed < duration:
            return frame
        elapsed -= duration
    raise v.Invalid('动作曝光序列无效')


def phase_profile(sequence, phase):
    """Rotate an exposure loop so `phase` becomes the first displayed moment.

    A phase inside an exposure splits that exposure into its remaining part at
    the start and the consumed part at the end, so the loop total stays the
    same as the unshifted loop. A phase on an exposure boundary only rotates.
    """
    total = sum(duration for _, duration in sequence)
    offset = phase % total
    if offset == 0:
        return list(sequence)
    elapsed = 0
    for index, (frame, duration) in enumerate(sequence):
        if offset < elapsed + duration:
            consumed = offset - elapsed
            rotated = []
            if consumed < duration:
                rotated.append((frame, duration - consumed))
            rotated.extend(sequence[index + 1:])
            rotated.extend(sequence[:index])
            if consumed > 0:
                rotated.append((frame, consumed))
            return rotated
        elapsed += duration
    raise v.Invalid('动作曝光序列无效')


def validate_map(project, tilemap):
    animations = tilemap.get('animations', {})
    if not isinstance(animations, dict):
        raise v.Invalid('动态瓦片定义无效')
    for base, clip_id in animations.items():
        if str(v.integer(int(base), '动态瓦片编号', 1, len(tilemap['tiles']))) != base:
            raise v.Invalid('动态瓦片编号无效')
        clip_schedule(project, clip_id)
    profiles = set()
    for layer in tilemap['layers']:
        offsets = layer.get('phase_offsets', {})
        if not isinstance(offsets, dict):
            raise v.Invalid('实例相位表无效')
        for cell, phase in offsets.items():
            if str(v.integer(int(cell), '地图格位置', 0, len(layer['data']) - 1)) != cell:
                raise v.Invalid('地图格位置无效')
            if isinstance(phase, bool) or not isinstance(phase, int):
                raise v.Invalid('实例相位须为整数毫秒')
            v.integer(phase, '实例相位', 0, 3000000)
        for index, raw in enumerate(layer['data']):
            base = raw & GID_MASK
            if str(base) in animations:
                sequence = clip_schedule(project, animations[str(base)])
                phase = offsets.get(str(index), 0) % sum(d for _, d in sequence)
                profiles.add((base, phase))
    if len(profiles) > 64:
        raise v.Invalid('每张地图最多64种动态瓦片与相位组合')


def bind_animation(store, key, revision, map_id, tile, clip_id):
    from .tilemaps import find_map
    with store.edit(key, revision, '设置动态瓦片') as project:
        tilemap = find_map(project, map_id)
        tile = v.integer(tile, '瓦片编号', 1, len(tilemap['tiles']))
        if clip_id:
            clip_schedule(project, clip_id)
            tilemap.setdefault('animations', {})[str(tile)] = clip_id
        else:
            tilemap.setdefault('animations', {}).pop(str(tile), None)
    return store.get(key)


def render_live(store, project, tilemap, elapsed=0):
    width, height = tilemap['tile_width'], tilemap['tile_height']
    result = Image.new('RGBA', (tilemap['columns'] * width, tilemap['rows'] * height))
    animations = {int(base): clip_schedule(project, key) for base, key in tilemap.get('animations', {}).items()}
    rasters = {}
    for layer in tilemap['layers']:
        if not layer['visible']:
            continue
        for index, raw in enumerate(layer['data']):
            base = raw & GID_MASK
            if not base:
                continue
            frame_id = tilemap['tiles'][base - 1]
            if base in animations:
                frame_id = frame_at(animations[base], elapsed + layer.get('phase_offsets', {}).get(str(index), 0))
            if frame_id not in rasters:
                rasters[frame_id] = render_frame(store, project, frame_id).resize((width, height), Image.Resampling.NEAREST)
            image = rasters[frame_id]
            if raw & 0x80000000:
                image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if raw & 0x40000000:
                image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            result.alpha_composite(image, (index % tilemap['columns'] * width, index // tilemap['columns'] * height))
    return result


def compile_resources(project, tilemap, base_document):
    # Keep one timed definition per source tile and per instance phase profile.
    # Image files stay shared, so repeated phases never duplicate pixels.
    document = deepcopy(base_document)
    tileset = document['tilesets'][0]
    firstgid = tileset['firstgid']
    definitions = tileset['tiles']
    sources = {}
    static_ids = {}
    base_paths = {}
    for index, frame_id in enumerate(tilemap['tiles']):
        base_paths.setdefault(frame_id, f'tiles/{index + 1}.png')

    def image_entry(identifier, frame_id, filename=None, phase=None):
        filename = filename or base_paths.get(frame_id, 'tiles/' + frame_id + '.png')
        sources[filename] = frame_id
        properties = [{'name': 'frameId', 'type': 'string', 'value': frame_id}]
        if phase is not None:
            properties.append({'name': 'phase', 'type': 'int', 'value': phase})
        return {'id': identifier, 'image': filename,
                'imagewidth': tilemap['tile_width'], 'imageheight': tilemap['tile_height'],
                'properties': properties}

    for index, frame_id in enumerate(tilemap['tiles']):
        definitions[index] = image_entry(index, frame_id, f'tiles/{index + 1}.png')

    def static_frame(frame_id):
        if frame_id not in static_ids:
            identifier = len(definitions)
            static_ids[frame_id] = identifier
            definitions.append(image_entry(identifier, frame_id))
        return static_ids[frame_id]

    animations = {int(base): clip_schedule(project, clip_id)
                  for base, clip_id in tilemap.get('animations', {}).items()}
    for base, sequence in animations.items():
        animation = [{'tileid': static_frame(frame), 'duration': duration} for frame, duration in sequence]
        definitions[base - 1]['animation'] = animation

    profiles = {}

    def profile_tile(base, phase):
        sequence = animations[base]
        total = sum(duration for _, duration in sequence)
        phase %= total
        if phase == 0:
            return base - 1
        key = (base, phase)
        if key not in profiles:
            identifier = len(definitions)
            entry = image_entry(identifier, tilemap['tiles'][base - 1], phase=phase)
            entry['animation'] = [{'tileid': static_frame(frame), 'duration': duration}
                                  for frame, duration in phase_profile(sequence, phase)]
            profiles[key] = identifier
            definitions.append(entry)
        return profiles[key]

    for layer, rendered in zip(tilemap['layers'], document['layers']):
        offsets = layer.get('phase_offsets', {})
        if not isinstance(offsets, dict):
            offsets = {}
        data = rendered['data']
        for index, raw in enumerate(data):
            base = raw & GID_MASK
            if base not in animations:
                continue
            data[index] = (raw & FLIP_MASK) | (profile_tile(base, offsets.get(str(index), 0)) + firstgid)
    tileset['tilecount'] = len(definitions)
    return document, sources


def render_resources(store, project, document, sources, elapsed=0):
    width, height = document['tilewidth'], document['tileheight']
    definitions = {item['id']: item for item in document['tilesets'][0]['tiles']}
    firstgid = document['tilesets'][0]['firstgid']
    result = Image.new('RGBA', (document['width'] * width, document['height'] * height))
    rasters = {}
    for layer in document['layers']:
        if not layer['visible']:
            continue
        for index, raw in enumerate(layer['data']):
            gid = raw & GID_MASK
            if not gid:
                continue
            entry = definitions[gid - firstgid]
            if entry.get('animation'):
                sequence = [(item['tileid'], item['duration']) for item in entry['animation']]
                entry = definitions[frame_at(sequence, elapsed)]
            filename = entry['image']
            if filename not in rasters:
                rasters[filename] = render_frame(store, project, sources[filename]).resize((width, height), Image.Resampling.NEAREST)
            image = rasters[filename]
            if raw & 0x80000000:
                image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if raw & 0x40000000:
                image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            result.alpha_composite(image, (index % document['width'] * width, index // document['width'] * height))
    return result
