import uuid
from copy import deepcopy
from . import validate as v


SCHEMA = 1


def new_id():
    return uuid.uuid4().hex


def new_layer(name, asset, **properties):
    layer = {
        'id': new_id(),
        'name': name,
        'asset': asset,
        'visible': True,
        'locked': False,
        'opacity': 1,
        'blend': 'normal',
        'transform': v.transform({}),
    }
    layer.update(properties)
    return layer


def new_frame(asset, name='帧', duration=100):
    return {
        'id': new_id(),
        'name': name,
        'duration': duration,
        'layers': [new_layer('绘制层', asset)],
        'pivot': [0.5, 0.5],
        'boxes': [],
        'notes': '',
    }


def new_project(name, width, height, asset):
    now = v.timestamp()
    return {
        'schema': SCHEMA,
        'id': new_id(),
        'name': name,
        'width': width,
        'height': height,
        'revision': 0,
        'created_at': now,
        'updated_at': now,
        'archived': False,
        'notes': '',
        'tags': [],
        'loop': 0,
        'frames': [new_frame(asset, '帧 1')],
        'clips': [],
        'palette': ['#142136ff', '#65e2b9ff', '#f8cc65ff', '#8c9bffff', '#fa87acff', '#ffffffff'],
        'background': '#00000000',
        'tilemaps': [],
        'guides': {'grid': 8, 'safe_margin': 0},
        'undo': [],
        'redo': [],
    }


def find_frame(project, frame_id):
    v.key(frame_id)
    frame = next((f for f in project['frames'] if f['id'] == frame_id), None)
    if frame is None:
        raise v.Invalid('动画帧不存在')
    return frame


def find_layer(project, frame_id, layer_id):
    frame = find_frame(project, frame_id)
    v.key(layer_id)
    from .layer_nodes import walk_layers
    layer = next((l for l in walk_layers(frame['layers']) if l['id'] == layer_id), None)
    if layer is None:
        raise v.Invalid('图层不存在')
    return frame, layer


def editable_layer(project, frame_id, layer_id):
    frame, layer = find_layer(project, frame_id, layer_id)
    if layer['locked']:
        raise v.Invalid('图层已锁定，请先解锁')
    return frame, layer


def clone_frame(frame):
    copied = deepcopy(frame)
    copied['id'] = new_id()
    copied['name'] = frame['name'][:85] + ' · 副本'
    from .layer_nodes import walk_layers
    for layer in walk_layers(copied['layers']):
        layer['id'] = new_id()
    return copied


def project_snapshot(project):
    return {key: deepcopy(value) for key, value in project.items() if key not in ('undo', 'redo', 'revision', 'updated_at')}


def public_summary(project):
    total_duration = sum(frame['duration'] for frame in project['frames'])
    return {
        'id': project['id'],
        'name': project['name'],
        'width': project['width'],
        'height': project['height'],
        'frame_count': len(project['frames']),
        'duration': total_duration,
        'revision': project['revision'],
        'created_at': project['created_at'],
        'updated_at': project['updated_at'],
        'archived': project['archived'],
        'tags': project['tags'],
        'thumbnail_frame': project['frames'][0]['id'],
    }


def validate_structure(project):
    if 'ink_roles' in project:
        from .role_tools import validate_entries
        validate_entries(project['ink_roles'])
    if project.get('schema') != SCHEMA:
        raise v.Invalid('不支持的项目版本')
    v.key(project.get('id'))
    width, height = v.dimensions(project)
    frames = v.array(project.get('frames'), '动画帧', 300)
    if not frames:
        raise v.Invalid('项目至少保留一帧')
    layer_count = 0
    seen = set()
    for frame in frames:
        frame_id = v.key(frame.get('id'))
        if frame_id in seen:
            raise v.Invalid('帧编号重复')
        seen.add(frame_id)
        v.frame_duration(frame.get('duration'), project)
        layers = v.array(frame.get('layers'), '图层', 16)
        if not layers:
            raise v.Invalid('每帧至少保留一个图层')
        seen_layers = set()
        from .layer_nodes import walk_layers
        for layer in walk_layers(layers):
            layer_id = v.key(layer.get('id'))
            if layer_id in seen_layers:
                raise v.Invalid('图层编号重复')
            seen_layers.add(layer_id)
            v.digest(layer.get('asset'))
            v.layer_properties(layer)
            layer_count += int(layer.get('kind') != 'group')
        pivot = v.array(frame.get('pivot'), '枢轴', 2)
        if len(pivot) != 2:
            raise v.Invalid('枢轴需要两个分量')
        for component in pivot:
            v.real(component, '枢轴', 0, 1)
    v.pixel_budget(width, height, layer_count)
    from .atlas_workspaces import validate_workspaces
    validate_workspaces(project)
    from .cels import validate as validate_cels
    validate_cels(project)
    from .contracts import validate_document
    validate_document(project)
    return project
