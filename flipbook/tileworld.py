"""大范围瓦片世界的稀疏区块存储。

旧地图使用 columns/rows + 每层定长 data；世界地图使用 mode='world'，
每层只保存包含实际瓦片的区块（chunk）。世界坐标是带符号的格坐标，
(0,0) 固定为世界原点，因此负坐标和远处坐标都不需要先扩展矩形。
"""

from . import validate as v


FLIP_HORIZONTAL = 0x80000000
FLIP_VERTICAL = 0x40000000
GID_MASK = 0x1FFFFFFF

DEFAULT_CHUNK = 16
COORDINATE_LIMIT = 1000000
MAX_CELLS = 250000
MAX_CHUNKS = 50000
MAX_REGION_PIXELS = 16000000
MAX_REGION_CELLS = 250000


def is_world(tilemap):
    return tilemap.get('mode') == 'world'


def chunk_size(tilemap):
    return tilemap.get('chunk_size', DEFAULT_CHUNK)


def chunk_key(cx, cy):
    return f'{cx},{cy}'


def parse_chunk_key(key):
    if not isinstance(key, str):
        raise v.Invalid('区块编号必须是文本')
    parts = key.split(',')
    if len(parts) != 2 or any(not part or part.strip() != part for part in parts):
        raise v.Invalid('区块坐标格式应为 cx,cy')
    try:
        cx, cy = int(parts[0]), int(parts[1])
    except ValueError as error:
        raise v.Invalid('区块坐标必须是整数') from error
    if str(cx) != parts[0] or str(cy) != parts[1]:
        raise v.Invalid('区块坐标格式应为 cx,cy')
    return cx, cy


def check_world_coordinate(value, name='世界坐标'):
    return v.integer(value, name, -COORDINATE_LIMIT, COORDINATE_LIMIT)


def check_chunk_coordinate(value, name='区块坐标'):
    limit = COORDINATE_LIMIT // DEFAULT_CHUNK + 1
    return v.integer(value, name, -limit, limit)


def cell_index(tilemap, x, y):
    size = chunk_size(tilemap)
    return (y % size) * size + (x % size)


def get_cell(layer, tilemap, x, y):
    chunks = layer.get('chunks')
    if not isinstance(chunks, dict):
        return 0
    chunk = chunks.get(chunk_key(x // chunk_size(tilemap), y // chunk_size(tilemap)))
    if not chunk:
        return 0
    return chunk.get(str(cell_index(tilemap, x, y)), 0)


def set_cell(layer, tilemap, x, y, value):
    chunks = layer.setdefault('chunks', {})
    key = chunk_key(x // chunk_size(tilemap), y // chunk_size(tilemap))
    chunk = chunks.get(key)
    index = str(cell_index(tilemap, x, y))
    if value:
        if chunk is None:
            chunk = {}
            chunks[key] = chunk
        chunk[index] = value
    elif chunk is not None:
        chunk.pop(index, None)
        if not chunk:
            chunks.pop(key, None)


def clear_cell(layer, tilemap, x, y):
    set_cell(layer, tilemap, x, y, 0)


def phase_key(tilemap, x, y):
    if is_world(tilemap):
        return f'{x},{y}'
    return str(y * tilemap['columns'] + x)


def phase_for(layer, tilemap, x, y):
    return layer.get('phase_offsets', {}).get(phase_key(tilemap, x, y), 0)


def set_phase(layer, tilemap, x, y, phase):
    offsets = layer.setdefault('phase_offsets', {})
    if phase:
        offsets[phase_key(tilemap, x, y)] = phase
    else:
        offsets.pop(phase_key(tilemap, x, y), None)
    if not offsets:
        layer.pop('phase_offsets', None)


def layer_cell_count(layer):
    if is_world_layer(layer):
        return sum(len(chunk) for chunk in layer.get('chunks', {}).values())
    return sum(1 for raw in layer.get('data', []) if raw)


def check_world_budget(tilemap, extra_cells=0):
    total_cells = 0
    total_chunks = 0
    for layer in tilemap['layers']:
        if is_world_layer(layer):
            total_cells += sum(len(chunk) for chunk in layer.get('chunks', {}).values())
            total_chunks += len(layer.get('chunks', {}))
        else:
            total_cells += sum(1 for raw in layer.get('data', []) if raw)
    if total_cells + extra_cells > MAX_CELLS:
        raise v.Invalid(f'地图已存储格子上限{MAX_CELLS}格，请清理已用格子或改用区域导出')
    if total_chunks > MAX_CHUNKS:
        raise v.Invalid('地图区块过多，请清理或缩小世界范围')


def is_world_layer(layer):
    return 'chunks' in layer


def iter_all_cells(tilemap, layer):
    if is_world(tilemap):
        size = chunk_size(tilemap)
        for key, chunk in layer.get('chunks', {}).items():
            cx, cy = parse_chunk_key(key)
            for local, raw in chunk.items():
                index = int(local)
                yield cx * size + index % size, cy * size + index // size, raw
    else:
        columns = tilemap['columns']
        for index, raw in enumerate(layer['data']):
            if raw:
                yield index % columns, index // columns, raw


def iter_region_cells(tilemap, layer, region):
    x0, y0, width, height = region
    x1, y1 = x0 + width, y0 + height
    if is_world(tilemap):
        size = chunk_size(tilemap)
        for key, chunk in layer.get('chunks', {}).items():
            cx, cy = parse_chunk_key(key)
            for local, raw in chunk.items():
                if not raw:
                    continue
                index = int(local)
                x = cx * size + index % size
                y = cy * size + index // size
                if x0 <= x < x1 and y0 <= y < y1:
                    yield x, y, raw
    else:
        columns = tilemap['columns']
        left = max(0, x0)
        right = min(columns, x1)
        top = max(0, y0)
        bottom = min(tilemap['rows'], y1)
        for y in range(top, bottom):
            for x in range(left, right):
                raw = layer['data'][y * columns + x]
                if raw:
                    yield x, y, raw


def used_bounds(tilemap):
    left = right = top = bottom = None
    for layer in tilemap['layers']:
        for x, y, _ in iter_all_cells(tilemap, layer):
            if left is None:
                left = right = x
                top = bottom = y
                continue
            left = min(left, x)
            right = max(right, x)
            top = min(top, y)
            bottom = max(bottom, y)
    if left is None:
        return None
    return [left, top, right - left + 1, bottom - top + 1]


def coordinate_range(tilemap):
    if is_world(tilemap):
        return -COORDINATE_LIMIT, COORDINATE_LIMIT
    return 0, tilemap['columns'] - 1, 0, tilemap['rows'] - 1


def validate_region(tilemap, region, pixels=True):
    x, y, width, height = region
    x = check_world_coordinate(x, '区域起点X')
    y = check_world_coordinate(y, '区域起点Y')
    width = v.integer(width, '区域列数', 1, 4096)
    height = v.integer(height, '区域行数', 1, 4096)
    if width * height > MAX_REGION_CELLS:
        raise v.Invalid('区域格数超过25万，请缩小可见范围')
    if pixels and width * height * tilemap['tile_width'] * tilemap['tile_height'] > MAX_REGION_PIXELS:
        raise v.Invalid('区域图像超过1600万像素，请缩小可见范围或放大视图')
    if x + width - 1 > COORDINATE_LIMIT or y + height - 1 > COORDINATE_LIMIT:
        raise v.Invalid('区域超出世界坐标范围')
    return x, y, width, height


def default_region(tilemap):
    if is_world(tilemap):
        return None
    return 0, 0, tilemap['columns'], tilemap['rows']


def clamp_region_to_map(tilemap, region):
    x, y, width, height = region
    if is_world(tilemap):
        return x, y, width, height
    left = max(0, x)
    right = min(tilemap['columns'], x + width)
    top = max(0, y)
    bottom = min(tilemap['rows'], y + height)
    if right <= left or bottom <= top:
        return x, y, 0, 0
    return left, top, right - left, bottom - top
