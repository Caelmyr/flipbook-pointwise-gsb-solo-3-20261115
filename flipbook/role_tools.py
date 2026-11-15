import numpy as np
from PIL import Image
from . import validate as v
from .model import editable_layer
from .drawing import draw, selection_mask, command_mask


DEFAULT_ROLES = [{'id': 11, 'name': '外衣', 'rgb': [180, 60, 60]},
                 {'id': 27, 'name': '阴影', 'rgb': [180, 60, 60]}]

ROLE_ENCODING = 'rgba-role-v1'
# 未归属像素的地图取值；其余取值为 ink_roles 中的角色编号（1..255）。
UNASSIGNED = 0


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


def role_map_asset(layer):
    return layer.get('role_map_asset')


def load_role_map(store, project, layer):
    """读取逐像素角色编号图（L 模式，0 表示尚未归属）。缺失时按未归属处理。"""
    asset = role_map_asset(layer)
    if not asset:
        return Image.new('L', (project['width'], project['height']), UNASSIGNED)
    return store.image(project['id'], asset).convert('L')


def save_role_map(store, project, layer, role_map):
    role_map = role_map.convert('L')
    layer['role_map_asset'] = store.put_image(project['id'], role_map)
    return role_map


def validate_role_layers(store, project):
    """结构校验：角色图层必须携带归属图，尺寸一致且取值合法。"""
    known = {role['id'] for role in entries(project)}
    size = (project['width'], project['height'])
    for frame in project['frames']:
        for layer in frame['layers']:
            if not layer.get('role_encoding'):
                continue
            asset = role_map_asset(layer)
            if not asset:
                raise v.Invalid('角色色图层缺少像素归属图')
            try:
                role_map = store.image(project['id'], asset).convert('L')
            except (OSError, ValueError) as error:
                raise v.Invalid('像素归属图无法读取') from error
            if role_map.size != size:
                raise v.Invalid('像素归属图尺寸与画布不一致')
            values = np.unique(np.asarray(role_map))
            unknown = [int(value) for value in values if value != UNASSIGNED and int(value) not in known]
            if unknown:
                raise v.Invalid('像素归属图引用了不存在的颜色角色')


def display_image(store, project, layer):
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
        # 已经是角色图层时保持原归属，不按当前颜色重新猜测。
        if not layer.get('role_encoding'):
            layer['role_encoding'] = ROLE_ENCODING
        if not role_map_asset(layer):
            save_role_map(store, project, layer,
                          Image.new('L', (project['width'], project['height']), UNASSIGNED))
    return store.get(key)


def update_roles(store, key, revision, roles):
    roles = validate_entries(roles)
    with store.edit(key, revision, '编辑角色色定义') as project:
        previous = entries(project)
        new_ids = {role['id'] for role in roles}
        # 归属永远跟随稳定的角色编号：编号仍在则保留，编号被删除才解除归属；
        # 新增角色没有历史像素，不能按当前颜色把别的角色像素猜成它。
        id_map = {old['id']: (old['id'] if old['id'] in new_ids else None)
                  for old in previous}
        rgb_by_id = {role['id']: role['rgb'] for role in roles}
        for frame in project['frames']:
            for layer in frame['layers']:
                if not layer.get('role_encoding'):
                    continue
                role_map = np.asarray(load_role_map(store, project, layer)).copy()
                for old_id, new_id in id_map.items():
                    if new_id is None:
                        role_map[role_map == old_id] = UNASSIGNED
                save_role_map(store, project, layer, Image.fromarray(role_map, 'L'))
                # 换色只依据持久化的角色编号，相同外观的不同角色不会被合并；
                # 已解除归属的像素保留当前外观，不参与任何角色的换色。
                pixels = np.asarray(store.image(key, layer['asset'])).copy()
                visible = pixels[:, :, 3] > 0
                for identifier, rgb in rgb_by_id.items():
                    pixels[visible & (role_map == identifier), :3] = np.asarray(rgb, dtype=np.uint8)
                layer['asset'] = store.put_image(key, Image.fromarray(pixels))
        project['ink_roles'] = roles
    return store.get(key)


def materialize(store, key, revision, frame_id, layer_id):
    with store.edit(key, revision, '角色色转普通图层') as project:
        _, layer = editable_layer(project, frame_id, layer_id)
        layer.pop('role_encoding', None)
        # 保留 role_map_asset 作为可撤销的历史引用；普通绘制不再读写它。
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
        pixels[selected, 3] = 255
        layer['asset'] = store.put_image(key, Image.fromarray(pixels))
        layer['role_encoding'] = ROLE_ENCODING
        role_map = np.asarray(load_role_map(store, project, layer)).copy()
        role_map[selected] = identifier
        save_role_map(store, project, layer, Image.fromarray(role_map, 'L'))
    return store.get(key)


def _apply_role_stroke(store, project, layer, role_map, identifier, image, command):
    """按笔迹落墨区域同步归属图，归属只认笔迹，不认当前颜色或颜色变化。"""
    painted = np.asarray(command_mask(image, command)) > 0
    role_map = np.asarray(role_map).copy()
    role_map[painted] = identifier
    # 被擦除到完全透明的像素清空归属；部分覆盖保留原角色。
    role_map[np.asarray(image)[:, :, 3] == 0] = UNASSIGNED
    return save_role_map(store, project, layer, Image.fromarray(role_map, 'L'))


def draw_role(store, project, layer, command, body):
    command = dict(command)
    if command['tool'] == 'gradient':
        raise v.Invalid('角色色不生成中间混色；可选择角色墨水或先转换为普通图层')
    before = store.image(project['id'], layer['asset'])
    role_map = load_role_map(store, project, layer)
    if command['tool'] == 'erase':
        image = draw(before, command)
        updated = np.asarray(role_map).copy()
        updated[np.asarray(image)[:, :, 3] == 0] = UNASSIGNED
        save_role_map(store, project, layer, Image.fromarray(updated, 'L'))
    else:
        identifier = v.integer(body.get('role_id'), '角色编号', 1, 255)
        role = next((r for r in entries(project) if r['id'] == identifier), None)
        if role is None:
            raise v.Invalid('角色不存在')
        alpha = v.integer(body.get('ink_alpha', 255), '墨水覆盖度', 0, 255)
        command['color'] = tuple(role['rgb']) + (alpha,)
        image = draw(before, command)
        role_map = _apply_role_stroke(store, project, layer, role_map, identifier, image, command)
    layer['asset'] = store.put_image(project['id'], image)
    layer['role_encoding'] = ROLE_ENCODING


def clear_role(store, project, layer):
    layer['asset'] = store.put_image(project['id'], Image.new('RGBA', (project['width'], project['height'])))
    save_role_map(store, project, layer,
                  Image.new('L', (project['width'], project['height']), UNASSIGNED))


def summary(store, project):
    result = []
    for frame in project['frames']:
        for layer in frame['layers']:
            if not layer.get('role_encoding'):
                continue
            pixels = np.asarray(store.image(project['id'], layer['asset']))
            role_map = np.asarray(load_role_map(store, project, layer))
            visible = pixels[:, :, 3] > 0
            unresolved = int(np.count_nonzero(visible & (role_map == UNASSIGNED)))
            result.append({'frame': frame['name'], 'layer': layer['name'],
                           'unresolved_visible_pixels': unresolved})
    return result
