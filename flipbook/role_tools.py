import numpy as np
from PIL import Image
from . import validate as v
from .model import editable_layer
from .drawing import draw, selection_mask


DEFAULT_ROLES = [{'id': 11, 'name': '外衣', 'rgb': [180, 60, 60]},
                 {'id': 27, 'name': '阴影', 'rgb': [180, 60, 60]}]


def entries(project):
    return project.get('ink_roles', DEFAULT_ROLES)


def validate_entries(value):
    value = v.array(value, '角色色', 255)
    if not value:
        raise v.Invalid('至少保留一个颜色角色')
    used = set()
    for role in value:
        identifier = v.integer(role.get('id'), '角色编号', 1, 255)
        if identifier in used:
            raise v.Invalid('角色编号重复')
        used.add(identifier)
        v.text(role.get('name'), '角色名称', 80, False)
        rgb = v.array(role.get('rgb'), '角色RGB', 3)
        if len(rgb) != 3:
            raise v.Invalid('角色色需要三个RGB分量')
        for component in rgb:
            v.integer(component, '颜色分量', 0, 255)
    return value


def display_image(store, project, layer):
    if layer.get('kind') == 'group':
        from .layer_tree import render_node_preview
        return render_node_preview(layer, (project['width'], project['height']),
                                   lambda asset: store.image(project['id'], asset))
    return store.image(project['id'], layer['asset'])


def require_ordinary(layer):
    if layer.get('role_encoding'):
        raise v.Invalid('此操作需要普通图层；可先转换并保留当前外观，原角色数据仍可撤销恢复')


def enable(store, key, revision, frame_id, layer_id):
    with store.edit(key, revision, '启用角色色墨水') as project:
        _, layer = editable_layer(project, frame_id, layer_id)
        from .cels import require_detached
        from .layer_nodes import require_leaf
        require_detached(layer)
        require_leaf(layer)
        project.setdefault('ink_roles', [dict(role) for role in DEFAULT_ROLES])
        layer['role_encoding'] = 'rgba-role-v1'
    return store.get(key)


def update_roles(store, key, revision, roles):
    roles = validate_entries(roles)
    with store.edit(key, revision, '编辑角色色定义') as project:
        previous = entries(project)
        palette = np.asarray([role['rgb'] for role in previous], dtype=float)
        replacement = {role['id']: role['rgb'] for role in roles}
        from .layer_nodes import walk_layers
        for frame in project['frames']:
            for layer in walk_layers(frame['layers']):
                if not layer.get('role_encoding'):
                    continue
                pixels = np.asarray(store.image(key, layer['asset'])).copy()
                original = pixels.copy()
                active = pixels[:, :, 3] > 0
                rgb, inverse = np.unique(original[active, :3], axis=0, return_inverse=True)
                if len(rgb):
                    closest = np.argmin(np.sum((rgb[:, None, :].astype(float) - palette[None, :, :]) ** 2, axis=2), axis=1)
                    mapped = []
                    for color, index in zip(rgb, closest):
                        mapped.append(replacement.get(previous[int(index)]['id'], color))
                    pixels[active, :3] = np.asarray(mapped, dtype=np.uint8)[inverse]
                layer['asset'] = store.put_image(key, Image.fromarray(pixels))
        project['ink_roles'] = roles
    return store.get(key)


def materialize(store, key, revision, frame_id, layer_id):
    with store.edit(key, revision, '角色色转普通图层') as project:
        _, layer = editable_layer(project, frame_id, layer_id)
        layer.pop('role_encoding', None)
    return store.get(key)


def assign_region(store, key, revision, frame_id, layer_id, body):
    with store.edit(key, revision, '明确指定像素颜色角色') as project:
        _, layer = editable_layer(project, frame_id, layer_id)
        if layer['transform'] != v.transform({}):
            raise v.Invalid('区域归属按原图层坐标指定，请先恢复图层变换或转换为普通图层')
        identifier = v.integer(body.get('role_id'), '角色编号', 1, 255)
        role = next((r for r in entries(project) if r['id'] == identifier), None)
        if role is None:
            raise v.Invalid('角色不存在')
        selected = np.asarray(selection_mask((project['width'], project['height']), body)) > 0
        pixels = np.asarray(store.image(key, layer['asset'])).copy()
        pixels[selected, :3] = role['rgb']
        layer['asset'] = store.put_image(key, Image.fromarray(pixels))
        layer['role_encoding'] = 'rgba-role-v1'
    return store.get(key)


def draw_role(store, project, layer, command, body):
    command = dict(command)
    if command['tool'] == 'gradient':
        raise v.Invalid('角色色不生成中间混色；可选择角色墨水或先转换为普通图层')
    if command['tool'] != 'erase':
        identifier = v.integer(body.get('role_id'), '角色编号', 1, 255)
        role = next((r for r in entries(project) if r['id'] == identifier), None)
        if role is None:
            raise v.Invalid('角色不存在')
        alpha = v.integer(body.get('ink_alpha', 255), '墨水覆盖度', 0, 255)
        command['color'] = tuple(role['rgb']) + (alpha,)
    image = draw(store.image(project['id'], layer['asset']), command)
    layer['asset'] = store.put_image(project['id'], image)


def clear_role(store, project, layer):
    layer['asset'] = store.put_image(project['id'], Image.new('RGBA', (project['width'], project['height'])))


def summary(store, project):
    from .layer_nodes import walk_layers
    return [{'frame': frame['name'], 'layer': layer['name'], 'unresolved_visible_pixels': 0}
            for frame in project['frames'] for layer in walk_layers(frame['layers']) if layer.get('role_encoding')]
