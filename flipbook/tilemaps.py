import random
from . import validate as v
from . import tileworld as tw
from .model import new_id


FLIP_HORIZONTAL = tw.FLIP_HORIZONTAL
FLIP_VERTICAL = tw.FLIP_VERTICAL


def find_map(project, map_id):
    v.key(map_id)
    result = next((m for m in project['tilemaps'] if m['id'] == map_id), None)
    if result is None:
        raise v.Invalid('瓦片地图不存在')
    return result


def map_dimensions(body, project):
    mode = v.choice(body.get('mode', 'finite'), '地图模式', ['finite', 'world'])
    tile_width = v.integer(body.get('tile_width', min(32, project['width'])), '瓦片宽度', 8, 256)
    tile_height = v.integer(body.get('tile_height', min(32, project['height'])), '瓦片高度', 8, 256)
    if mode == 'world':
        chunk_size = v.integer(body.get('chunk_size', tw.DEFAULT_CHUNK), '区块边长', 8, 64)
        if chunk_size * tile_width * chunk_size * tile_height > tw.MAX_REGION_PIXELS:
            raise v.Invalid('单个区块渲染超过1600万像素')
        return mode, None, None, tile_width, tile_height, chunk_size
    columns = v.integer(body.get('columns', 12), '地图列数', 1, 100)
    rows = v.integer(body.get('rows', 8), '地图行数', 1, 100)
    if columns * rows * tile_width * tile_height > 16000000:
        raise v.Invalid('地图渲染超过1600万像素')
    return mode, columns, rows, tile_width, tile_height, None


def body_region(tilemap, body, required=False):
    """读取显式矩形区域 [x, y, width, height]。"""
    raw = body.get('region')
    if raw is None:
        if required:
            raise v.Invalid('大世界填充必须明确指定处理区域：x、y、width、height')
        return None
    if isinstance(raw, dict):
        values = [raw.get('x'), raw.get('y'), raw.get('width'), raw.get('height')]
        if any(item is None for item in values):
            raise v.Invalid('区域需要 x、y、width、height')
    else:
        values = v.array(raw, '区域', 4)
        if len(values) != 4:
            raise v.Invalid('区域需要 x、y、width、height')
    return tw.validate_region(tilemap, values, pixels=False)


def new_layer(name, tilemap):
    if tw.is_world(tilemap):
        return {'id': new_id(), 'name': name, 'visible': True, 'chunks': {}}
    return {'id': new_id(), 'name': name, 'visible': True,
            'data': [0] * (tilemap['columns'] * tilemap['rows'])}


class Tilemaps:
    def __init__(self, store):
        self.store = store

    def create(self, project_id, revision, body):
        with self.store.edit(project_id, revision, '新建瓦片地图') as project:
            if len(project['tilemaps']) >= 20:
                raise v.Invalid('每项目最多20张地图')
            mode, columns, rows, tw, th, chunk_size = map_dimensions(body, project)
            ids = v.frame_ids(body.get('tiles', [f['id'] for f in project['frames']]), project)
            if len(ids) > 256:
                raise v.Invalid('瓦片集合最多256张')
            item = {
                'id': new_id(),
                'name': v.text(body.get('name', '新地图'), '地图名称', 100, False),
                'mode': mode,
                'tile_width': tw,
                'tile_height': th,
                'tiles': ids,
            }
            if mode == 'world':
                item['chunk_size'] = chunk_size
                item['origin'] = [0, 0]
                item['layers'] = [new_layer('地形', item)]
            else:
                item['columns'] = columns
                item['rows'] = rows
                item['layers'] = [{'id': new_id(), 'name': '地形', 'visible': True,
                                   'data': [0] * (columns * rows)}]
            project['tilemaps'].append(item)
        return self.store.get(project_id)

    def paint(self, project_id, revision, map_id, body):
        with self.store.edit(project_id, revision, '绘制瓦片地图') as project:
            tilemap = find_map(project, map_id)
            layer_id = body.get('layer_id', tilemap['layers'][0]['id'])
            layer = next((l for l in tilemap['layers'] if l['id'] == layer_id), None)
            if layer is None:
                raise v.Invalid('地图图层不存在')
            value = v.integer(body.get('tile', 1), '瓦片编号（0为空）', 0, len(tilemap['tiles']))
            if value:
                if v.boolean(body.get('flip_x', False), '水平翻转'):
                    value |= tw.FLIP_HORIZONTAL
                if v.boolean(body.get('flip_y', False), '垂直翻转'):
                    value |= tw.FLIP_VERTICAL
            phase = v.integer(body.get('phase_ms', 0), '实例相位', 0, 3000000)
            cells = v.array(body.get('cells'), '地图坐标', 50000)
            if tw.is_world(tilemap):
                for point in cells:
                    x, y = self._world_point(point)
                    tw.set_cell(layer, tilemap, x, y, value)
                    tw.set_phase(layer, tilemap, x, y, phase)
                tw.check_world_budget(tilemap)
            else:
                for point in cells:
                    position = v.point(point, tilemap['columns'], tilemap['rows'])
                    x, y = [int(a) for a in position]
                    index = y * tilemap['columns'] + x
                    layer['data'][index] = value
                    if phase:
                        layer.setdefault('phase_offsets', {})[str(index)] = phase
                    else:
                        layer.setdefault('phase_offsets', {}).pop(str(index), None)
        return self.store.get(project_id)

    def _world_point(self, point):
        values = v.array(point, '世界坐标', 2)
        if len(values) != 2:
            raise v.Invalid('世界坐标需要 x 和 y')
        return (tw.check_world_coordinate(values[0], '世界坐标X'),
                tw.check_world_coordinate(values[1], '世界坐标Y'))

    def fill(self, project_id, revision, map_id, body):
        with self.store.edit(project_id, revision, '填充瓦片地图') as project:
            tilemap = find_map(project, map_id)
            layer = tilemap['layers'][0]
            mode = v.choice(body.get('mode', 'checker'), '填充模式', ['solid', 'checker', 'random', 'border'])
            first = v.integer(body.get('first', 1), '首瓦片编号', 0, len(tilemap['tiles']))
            second = v.integer(body.get('second', min(2, len(tilemap['tiles']))), '次瓦片编号', 0, len(tilemap['tiles']))
            seed = v.integer(body.get('seed', 42), '随机种子', 0, 1000000)
            layer_id = body.get('layer_id')
            if layer_id:
                layer = next((l for l in tilemap['layers'] if l['id'] == layer_id), None)
                if layer is None:
                    raise v.Invalid('地图图层不存在')
            region = body_region(tilemap, body, required=tw.is_world(tilemap))
            if region is not None and tw.is_world(tilemap):
                x0, y0, width, height = region
                if width * height > tw.MAX_CELLS:
                    raise v.Invalid('填充区域超过25万格，请缩小区域')
            if region is None:
                region = (0, 0, tilemap['columns'], tilemap['rows'])
            else:
                region = tw.clamp_region_to_map(tilemap, region)
                if region[2] == 0 or region[3] == 0:
                    raise v.Invalid('填充区域不在地图范围内')
            rng = random.Random(seed)
            x0, y0, width, height = region
            if tw.is_world(tilemap):
                # 只清除填充区域内的相位，区域外已设置的实例相位保持不变。
                for cell in list(layer.get('phase_offsets', {})):
                    try:
                        cell_x, cell_y = [int(part) for part in cell.split(',')]
                    except ValueError:
                        continue
                    if x0 <= cell_x < x0 + width and y0 <= cell_y < y0 + height:
                        layer['phase_offsets'].pop(cell, None)
            else:
                layer.pop('phase_offsets', None)
            for row in range(height):
                y = y0 + row
                for column in range(width):
                    x = x0 + column
                    if mode == 'solid':
                        value = first
                    elif mode == 'checker':
                        value = first if (x + y) % 2 == 0 else second
                    elif mode == 'random':
                        value = rng.choice([first, second])
                    else:
                        value = first if x in (x0, x0 + width - 1) or y in (y0, y0 + height - 1) else second
                    if tw.is_world(tilemap):
                        tw.set_cell(layer, tilemap, x, y, value)
                    else:
                        layer['data'][y * tilemap['columns'] + x] = value
            if tw.is_world(tilemap):
                tw.check_world_budget(tilemap)
        return self.store.get(project_id)

    def layer(self, project_id, revision, map_id, body):
        with self.store.edit(project_id, revision, '地图图层') as project:
            tilemap = find_map(project, map_id)
            if len(tilemap['layers']) >= 8:
                raise v.Invalid('每张地图最多8层')
            tilemap['layers'].append(new_layer(v.text(body.get('name', '新地图图层'), '地图图层名称', 100, False), tilemap))
        return self.store.get(project_id)

    def render(self, project_id, map_id, elapsed=0, region=None):
        from .map_animation import render_live
        project = self.store.get(project_id)
        tilemap = find_map(project, map_id)
        region = self._resolved_region(tilemap, region)
        return render_live(self.store, project, tilemap, elapsed, region)

    def render_export(self, project_id, map_id, elapsed=0, region=None):
        from .map_animation import compile_resources, render_resources
        project = self.store.get(project_id)
        tilemap = find_map(project, map_id)
        region = self._resolved_region(tilemap, region)
        document, sources = compile_resources(project, tilemap, self._base_document(project, tilemap, region))
        return render_resources(self.store, project, document, sources, elapsed)

    def tiled_json(self, project_id, map_id, region=None):
        from .map_animation import compile_resources
        project = self.store.get(project_id)
        tilemap = find_map(project, map_id)
        region = self._resolved_region(tilemap, region)
        return compile_resources(project, tilemap, self._base_document(project, tilemap, region))[0]

    def _resolved_region(self, tilemap, region):
        if region is not None:
            region = tuple(region)
            if tw.is_world(tilemap):
                return tw.validate_region(tilemap, region)
            clamped = tw.clamp_region_to_map(tilemap, region)
            if not clamped[2] or not clamped[3]:
                raise v.Invalid('查看区域不在地图范围内')
            return clamped
        if tw.is_world(tilemap):
            raise v.Invalid('大世界地图需要提供当前可见区域（region=[x, y, 列数, 行数]）')
        return None

    def stats(self, project_id, map_id):
        project = self.store.get(project_id)
        tilemap = find_map(project, map_id)
        layers = []
        for layer in tilemap['layers']:
            layers.append({
                'id': layer['id'],
                'name': layer['name'],
                'visible': layer['visible'],
                'cells': tw.layer_cell_count(layer),
                'chunks': len(layer.get('chunks', {})) if tw.is_world(tilemap) else 0,
                'phases': len(layer.get('phase_offsets', {})),
            })
        return {
            'mode': tilemap.get('mode', 'finite'),
            'tile_width': tilemap['tile_width'],
            'tile_height': tilemap['tile_height'],
            'chunk_size': tw.chunk_size(tilemap) if tw.is_world(tilemap) else 0,
            'origin': list(tilemap.get('origin', [0, 0])) if tw.is_world(tilemap) else [0, 0],
            'columns': tilemap.get('columns') if not tw.is_world(tilemap) else None,
            'rows': tilemap.get('rows') if not tw.is_world(tilemap) else None,
            'used_bounds': tw.used_bounds(tilemap),
            'layers': layers,
            'stored_cells': sum(item['cells'] for item in layers),
            'stored_chunks': sum(item['chunks'] for item in layers),
            'cell_limit': tw.MAX_CELLS,
            'chunk_limit': tw.MAX_CHUNKS,
            'cell_budget_remaining': max(0, tw.MAX_CELLS - sum(item['cells'] for item in layers)),
        }

    def _base_document(self, project, tilemap, region=None):
        layers = []
        for index, layer in enumerate(tilemap['layers'], 1):
            if tw.is_world(tilemap):
                if region is None:
                    raise v.Invalid('大世界地图导出需要明确区域')
                x0, y0, width, height = region
                data = [0] * (width * height)
                for x, y, raw in tw.iter_region_cells(tilemap, layer, region):
                    data[(y - y0) * width + (x - x0)] = raw
            else:
                if region is None:
                    x0 = y0 = 0
                    width, height = tilemap['columns'], tilemap['rows']
                else:
                    x0, y0, width, height = region
                data = [0] * (width * height)
                for x, y, raw in tw.iter_region_cells(tilemap, layer, (x0, y0, width, height)):
                    data[(y - y0) * width + (x - x0)] = raw
            layers.append({
                'id': index,
                'name': layer['name'],
                'type': 'tilelayer',
                'width': width,
                'height': height,
                'visible': layer['visible'],
                'opacity': 1,
                'x': 0,
                'y': 0,
                'data': data,
            })
        tile_entries = []
        for index, frame_id in enumerate(tilemap['tiles']):
            tile_entries.append({
                'id': index,
                'image': f'tiles/{index + 1}.png',
                'imagewidth': tilemap['tile_width'],
                'imageheight': tilemap['tile_height'],
                'properties': [{'name': 'frameId', 'type': 'string', 'value': frame_id}],
            })
        document_width = tilemap['columns'] if not tw.is_world(tilemap) else region[2]
        document_height = tilemap['rows'] if not tw.is_world(tilemap) else region[3]
        document = {
            'type': 'map',
            'version': '1.10',
            'tiledversion': '1.10.2',
            'orientation': 'orthogonal',
            'renderorder': 'right-down',
            'width': document_width,
            'height': document_height,
            'tilewidth': tilemap['tile_width'],
            'tileheight': tilemap['tile_height'],
            'infinite': False,
            'layers': layers,
            'tilesets': [{
                'firstgid': 1,
                'name': tilemap['name'],
                'tilewidth': tilemap['tile_width'],
                'tileheight': tilemap['tile_height'],
                'tilecount': len(tile_entries),
                'columns': 0,
                'tiles': tile_entries,
            }],
        }
        if tw.is_world(tilemap):
            x0, y0, width, height = region
            origin = list(tilemap.get('origin', [0, 0]))
            document['world'] = {
                'origin': origin,
                'region': {'x': x0, 'y': y0, 'width': width, 'height': height},
                'chunk_size': tw.chunk_size(tilemap),
            }
            document['properties'] = [
                {'name': 'flipbookWorldOrigin', 'type': 'string', 'value': f'{origin[0]},{origin[1]}'},
                {'name': 'flipbookRegion', 'type': 'string', 'value': f'{x0},{y0},{width},{height}'},
                {'name': 'flipbookCellFormula', 'type': 'string',
                 'value': f'absolute_cell = [{x0} + index % {width}, {y0} + index // {width}]'},
                {'name': 'flipbookPreviewAnchor', 'type': 'string', 'value': f'{x0},{y0}'},
            ]
        return document
