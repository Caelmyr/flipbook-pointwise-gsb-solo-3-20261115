import random
from PIL import Image
from . import validate as v
from .model import new_id, find_frame
from .composite import render_frame


FLIP_HORIZONTAL = 0x80000000
FLIP_VERTICAL = 0x40000000


def find_map(project, map_id):
    v.key(map_id)
    result = next((m for m in project['tilemaps'] if m['id'] == map_id), None)
    if result is None:
        raise v.Invalid('瓦片地图不存在')
    return result


def map_dimensions(body, project):
    columns = v.integer(body.get('columns', 12), '地图列数', 1, 100)
    rows = v.integer(body.get('rows', 8), '地图行数', 1, 100)
    tile_width = v.integer(body.get('tile_width', min(32, project['width'])), '瓦片宽度', 8, 256)
    tile_height = v.integer(body.get('tile_height', min(32, project['height'])), '瓦片高度', 8, 256)
    if columns * rows * tile_width * tile_height > 16000000:
        raise v.Invalid('地图渲染超过1600万像素')
    return columns, rows, tile_width, tile_height


class Tilemaps:
    def __init__(self, store):
        self.store = store

    def create(self, project_id, revision, body):
        with self.store.edit(project_id, revision, '新建瓦片地图') as project:
            if len(project['tilemaps']) >= 20:
                raise v.Invalid('每项目最多20张地图')
            columns, rows, tw, th = map_dimensions(body, project)
            ids = v.frame_ids(body.get('tiles', [f['id'] for f in project['frames']]), project)
            if len(ids) > 256:
                raise v.Invalid('瓦片集合最多256张')
            item = {
                'id': new_id(),
                'name': v.text(body.get('name', '新地图'), '地图名称', 100, False),
                'columns': columns,
                'rows': rows,
                'tile_width': tw,
                'tile_height': th,
                'tiles': ids,
                'layers': [{'id': new_id(), 'name': '地形', 'visible': True, 'data': [0] * (columns * rows)}],
            }
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
                    value |= FLIP_HORIZONTAL
                if v.boolean(body.get('flip_y', False), '垂直翻转'):
                    value |= FLIP_VERTICAL
            phase = v.integer(body.get('phase_ms', 0), '实例相位', 0, 3000000)
            cells = v.array(body.get('cells'), '地图坐标', 10000)
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

    def fill(self, project_id, revision, map_id, body):
        with self.store.edit(project_id, revision, '填充瓦片地图') as project:
            tilemap = find_map(project, map_id)
            layer = tilemap['layers'][0]
            mode = v.choice(body.get('mode', 'checker'), '填充模式', ['solid', 'checker', 'random', 'border'])
            first = v.integer(body.get('first', 1), '首瓦片编号', 0, len(tilemap['tiles']))
            second = v.integer(body.get('second', min(2, len(tilemap['tiles']))), '次瓦片编号', 0, len(tilemap['tiles']))
            seed = v.integer(body.get('seed', 42), '随机种子', 0, 1000000)
            rng = random.Random(seed)
            layer.pop('phase_offsets', None)
            for y in range(tilemap['rows']):
                for x in range(tilemap['columns']):
                    if mode == 'solid':
                        value = first
                    elif mode == 'checker':
                        value = first if (x + y) % 2 == 0 else second
                    elif mode == 'random':
                        value = rng.choice([first, second])
                    else:
                        value = first if x in (0, tilemap['columns'] - 1) or y in (0, tilemap['rows'] - 1) else second
                    layer['data'][y * tilemap['columns'] + x] = value
        return self.store.get(project_id)

    def layer(self, project_id, revision, map_id, body):
        with self.store.edit(project_id, revision, '地图图层') as project:
            tilemap = find_map(project, map_id)
            if len(tilemap['layers']) >= 8:
                raise v.Invalid('每张地图最多8层')
            tilemap['layers'].append({
                'id': new_id(),
                'name': v.text(body.get('name', '新地图图层'), '地图图层名称', 100, False),
                'visible': True,
                'data': [0] * (tilemap['columns'] * tilemap['rows']),
            })
        return self.store.get(project_id)

    def render(self, project_id, map_id, elapsed=0):
        from .map_animation import render_live
        project = self.store.get(project_id)
        return render_live(self.store, project, find_map(project, map_id), elapsed)

    def render_export(self, project_id, map_id, elapsed=0):
        from .map_animation import compile_resources, render_resources
        project = self.store.get(project_id)
        tilemap = find_map(project, map_id)
        document, sources = compile_resources(project, tilemap, self._base_document(project, tilemap))
        return render_resources(self.store, project, document, sources, elapsed)

    def tiled_json(self, project_id, map_id):
        from .map_animation import compile_resources
        project = self.store.get(project_id)
        tilemap = find_map(project, map_id)
        return compile_resources(project, tilemap, self._base_document(project, tilemap))[0]

    def _base_document(self, project, tilemap):
        layers = []
        for index, layer in enumerate(tilemap['layers'], 1):
            layers.append({
                'id': index,
                'name': layer['name'],
                'type': 'tilelayer',
                'width': tilemap['columns'],
                'height': tilemap['rows'],
                'visible': layer['visible'],
                'opacity': 1,
                'x': 0,
                'y': 0,
                'data': layer['data'],
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
        return {
            'type': 'map',
            'version': '1.10',
            'tiledversion': '1.10.2',
            'orientation': 'orthogonal',
            'renderorder': 'right-down',
            'width': tilemap['columns'],
            'height': tilemap['rows'],
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
