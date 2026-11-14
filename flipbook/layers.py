from copy import deepcopy
from PIL import Image
from . import validate as v
from .model import find_frame, find_layer, editable_layer, new_layer, new_id
from .composite import transform_layer, blend_pixels, render_frame
from .drawing import draw, move_selection, stamp
from .pixels import apply_effect
from .layer_nodes import walk_layers, require_leaf, parent_layers


class Layers:
    def __init__(self, store):
        self.store = store

    def add(self, project_id, revision, frame_id, name='新图层', color='#00000000'):
        with self.store.edit(project_id, revision, '添加图层') as project:
            frame = find_frame(project, frame_id)
            if len(list(walk_layers(frame['layers']))) >= 32:
                raise v.Invalid('每帧最多16个图层')
            image = Image.new('RGBA', (project['width'], project['height']), v.rgba(color))
            asset = self.store.put_image(project_id, image)
            frame['layers'].append(new_layer(v.text(name, '图层名称', 100, False), asset))
        return self.store.get(project_id)

    def update(self, project_id, revision, frame_id, layer_id, body):
        properties = v.layer_properties(body)
        with self.store.edit(project_id, revision, '图层属性') as project:
            _, layer = find_layer(project, frame_id, layer_id)
            layer.update(properties)
        return self.store.get(project_id)

    def remove(self, project_id, revision, frame_id, layer_id):
        with self.store.edit(project_id, revision, '移除图层') as project:
            frame, layer = editable_layer(project, frame_id, layer_id)
            if len(frame['layers']) <= 1:
                raise v.Invalid('每帧至少保留一个图层')
            parent_layers(frame, layer_id).remove(layer)
        return self.store.get(project_id)

    def duplicate(self, project_id, revision, frame_id, layer_id):
        with self.store.edit(project_id, revision, '复制图层') as project:
            frame, layer = find_layer(project, frame_id, layer_id)
            if len(list(walk_layers(frame['layers']))) >= 32:
                raise v.Invalid('图层数量超限')
            copied = deepcopy(layer)
            for item in walk_layers([copied]):
                item['id'] = new_id()
            copied['name'] = copied['name'][:85] + ' · 副本'
            copied['locked'] = False
            siblings = parent_layers(frame, layer_id)
            index = siblings.index(layer)
            siblings.insert(index + 1, copied)
        return self.store.get(project_id)

    def reorder(self, project_id, revision, frame_id, ids):
        with self.store.edit(project_id, revision, '图层排序') as project:
            frame = find_frame(project, frame_id)
            if len(ids) != len(frame['layers']) or set(ids) != {l['id'] for l in frame['layers']}:
                raise v.Invalid('排序必须包含全部图层且不能重复')
            lookup = {layer['id']: layer for layer in frame['layers']}
            frame['layers'] = [lookup[key] for key in ids]
        return self.store.get(project_id)

    def bake(self, project_id, revision, frame_id, layer_id):
        with self.store.edit(project_id, revision, '烘焙图层变换') as project:
            _, layer = editable_layer(project, frame_id, layer_id)
            require_leaf(layer)
            from .role_tools import require_ordinary
            require_ordinary(layer)
            image = self.store.image(project_id, layer['asset'])
            if layer.get('affine'):
                from .layer_tree import local_matrix, rasterize
                import numpy as np
                size = (project['width'], project['height'])
                image = rasterize(image, local_matrix(layer['transform'], size) @ np.asarray(layer['affine']), size)
            else:
                image = transform_layer(image, layer['transform'], (project['width'], project['height']))
            layer['asset'] = self.store.put_image(project_id, image)
            layer['transform'] = v.transform({})
            layer.pop('affine', None)
            layer.pop('cel_id', None)
        return self.store.get(project_id)

    def merge_down(self, project_id, revision, frame_id, layer_id):
        with self.store.edit(project_id, revision, '向下合并图层') as project:
            frame, layer = editable_layer(project, frame_id, layer_id)
            from .role_tools import require_ordinary
            require_ordinary(layer)
            from .cels import require_detached
            require_detached(layer)
            index = frame['layers'].index(layer)
            if index == 0:
                raise v.Invalid('底层没有可向下合并的图层')
            below = frame['layers'][index - 1]
            from .role_tools import require_ordinary
            require_ordinary(below)
            require_detached(below)
            if below['locked']:
                raise v.Invalid('下方图层已锁定')
            if below.get('kind') == 'group' or layer.get('kind') == 'group':
                raise v.Invalid('请先展开组合，再整理相邻子层')
            if below['blend'] != 'normal' or layer['blend'] != 'normal':
                if any(item['locked'] for item in [below, layer]):
                    raise v.Invalid('组合中包含锁定图层')
                blank = Image.new('RGBA', (project['width'], project['height']))
                grouped = new_layer('混合组合', self.store.put_image(project_id, blank))
                grouped.update(kind='group', children=[deepcopy(below), deepcopy(layer)], passthrough=True)
                frame['layers'][index - 1:index + 1] = [grouped]
                return_after_edit = True
            else:
                return_after_edit = False
            if not return_after_edit:
                size = (project['width'], project['height'])
                base = Image.new('RGBA', size)
                for current in [below, layer]:
                    if not current['visible']:
                        continue
                    image = transform_layer(self.store.image(project_id, current['asset']), current['transform'], size)
                    base = blend_pixels(base, image, current['blend'], current['opacity'])
                below['asset'] = self.store.put_image(project_id, base)
                below['visible'] = True
                below['opacity'] = 1
                below['transform'] = v.transform({})
                parent_layers(frame, layer_id).remove(layer)
        return self.store.get(project_id)

    def flatten(self, project_id, revision, frame_ids):
        with self.store.edit(project_id, revision, '合并可见图层') as project:
            ids = v.frame_ids(frame_ids, project)
            for frame_id in ids:
                frame = find_frame(project, frame_id)
                if any(layer['locked'] for layer in walk_layers(frame['layers'])):
                    raise v.Invalid('包含锁定图层，请先解锁')
                image = render_frame(self.store, project, frame_id)
                frame['layers'] = [new_layer('合成层', self.store.put_image(project_id, image))]
        return self.store.get(project_id)

    def require_identity(self, layer):
        require_leaf(layer)
        if layer.get('affine') or layer['transform'] != v.transform({}):
            raise v.Invalid('请先烘焙图层变换后编辑像素，避免坐标不一致')

    def draw(self, project_id, revision, frame_id, layer_id, body):
        with self.store.edit(project_id, revision, '像素绘制') as project:
            _, layer = editable_layer(project, frame_id, layer_id)
            command = v.draw_command(body, project['width'], project['height'])
            if layer.get('cel_id'):
                from .cels import draw_shared
                draw_shared(self.store, project, layer, command)
            elif layer.get('role_encoding'):
                from .role_tools import draw_role
                draw_role(self.store, project, layer, command, body)
            else:
                self.require_identity(layer)
                image = self.store.image(project_id, layer['asset'])
                image = draw(image, command)
                layer['asset'] = self.store.put_image(project_id, image)
        return self.store.get(project_id)

    def clear(self, project_id, revision, frame_id, layer_id):
        with self.store.edit(project_id, revision, '清空图层') as project:
            _, layer = editable_layer(project, frame_id, layer_id)
            require_leaf(layer)
            if layer.get('cel_id'):
                from .cels import clear_shared
                clear_shared(self.store, project, layer)
            elif layer.get('role_encoding'):
                from .role_tools import clear_role
                clear_role(self.store, project, layer)
            else:
                blank = Image.new('RGBA', (project['width'], project['height']))
                layer['asset'] = self.store.put_image(project_id, blank)
        return self.store.get(project_id)

    def effect(self, project_id, revision, ids, layer_id, body):
        with self.store.edit(project_id, revision, '像素效果') as project:
            ids = v.frame_ids(ids, project)
            if len(ids) > 1:
                layer_name = None
                for frame in project['frames']:
                    matching = next((l for l in frame['layers'] if l['id'] == layer_id), None)
                    if matching:
                        layer_name = matching['name']
                        break
                if layer_name is None:
                    raise v.Invalid('找不到用于批量匹配的源图层')
            for frame_id in ids:
                frame = find_frame(project, frame_id)
                if len(ids) == 1:
                    _, layer = editable_layer(project, frame_id, layer_id)
                    from .role_tools import require_ordinary
                    require_ordinary(layer)
                else:
                    matches = [l for l in frame['layers'] if l['name'] == layer_name]
                    if len(matches) != 1:
                        raise v.Invalid('批量效果要求各帧有唯一同名图层')
                    layer = matches[0]
                    if layer['locked']:
                        raise v.Invalid('批量目标中有锁定图层')
                require_leaf(layer)
                from .role_tools import require_ordinary
                require_ordinary(layer)
                from .cels import require_detached
                require_detached(layer)
                image = apply_effect(self.store.image(project_id, layer['asset']), body)
                layer['asset'] = self.store.put_image(project_id, image)
        return self.store.get(project_id)

    def selection(self, project_id, revision, frame_id, layer_id, body):
        with self.store.edit(project_id, revision, '移动像素选区') as project:
            _, layer = editable_layer(project, frame_id, layer_id)
            from .role_tools import require_ordinary
            require_ordinary(layer)
            from .cels import require_detached
            require_detached(layer)
            self.require_identity(layer)
            selection = v.object_value(body.get('selection'), '选区')
            dx = v.integer(body.get('dx', 0), '选区偏移X', -2048, 2048)
            dy = v.integer(body.get('dy', 0), '选区偏移Y', -2048, 2048)
            copy = v.boolean(body.get('copy', False), '复制选区')
            image = self.store.image(project_id, layer['asset'])
            layer['asset'] = self.store.put_image(project_id, move_selection(image, selection, dx, dy, copy))
        return self.store.get(project_id)

    def propagate(self, project_id, revision, source_frame, layer_id, target_ids, mode='append'):
        with self.store.edit(project_id, revision, '跨帧复制图层') as project:
            _, source = find_layer(project, source_frame, layer_id)
            ids = v.frame_ids(target_ids, project)
            v.choice(mode, '复制方式', ['append', 'replace_name'])
            for frame_id in ids:
                if frame_id == source_frame:
                    continue
                frame = find_frame(project, frame_id)
                copied = deepcopy(source)
                for item in walk_layers([copied]):
                    item['id'] = new_id()
                copied['locked'] = False
                copied.pop('cel_id', None)
                matches = [l for l in frame['layers'] if l['name'] == source['name']]
                if mode == 'replace_name' and matches:
                    if len(matches) != 1 or matches[0]['locked']:
                        raise v.Invalid('同名图层不唯一或已锁定')
                    frame['layers'][frame['layers'].index(matches[0])] = copied
                else:
                    if len(list(walk_layers(frame['layers']))) >= 32:
                        raise v.Invalid('目标帧图层数量超限')
                    frame['layers'].append(copied)
        return self.store.get(project_id)

    def stamp(self, project_id, revision, frame_id, layer_id, body):
        with self.store.edit(project_id, revision, '帧图像印章') as project:
            _, layer = editable_layer(project, frame_id, layer_id)
            from .role_tools import require_ordinary
            require_ordinary(layer)
            from .cels import require_detached
            require_detached(layer)
            self.require_identity(layer)
            source_id = v.key(body.get('source_frame'))
            source = render_frame(self.store, project, source_id)
            x = v.integer(body.get('x', 0), '印章X', -2048, 2048)
            y = v.integer(body.get('y', 0), '印章Y', -2048, 2048)
            scale = v.real(body.get('scale', 1), '印章比例', .05, 4)
            rotation = v.real(body.get('rotation', 0), '印章旋转', -360, 360)
            opacity = v.real(body.get('opacity', 1), '印章透明度', 0, 1)
            image = stamp(self.store.image(project_id, layer['asset']), source, x, y, scale, rotation, opacity)
            layer['asset'] = self.store.put_image(project_id, image)
        return self.store.get(project_id)

    def ungroup(self, project_id, revision, frame_id, layer_id):
        with self.store.edit(project_id, revision, '解开混合组合') as project:
            frame, group = editable_layer(project, frame_id, layer_id)
            if group.get('kind') != 'group':
                raise v.Invalid('所选图层不是组合')
            if group['opacity'] != 1:
                raise v.Invalid('解组前请恢复组合整体透明度；内部原图层仍保留')
            if not group['visible']:
                raise v.Invalid('请先显示组合再解组')
            from .layer_tree import local_matrix
            import numpy as np
            size = (project['width'], project['height'])
            parent = local_matrix(group['transform'], size) @ np.asarray(group.get('affine', np.eye(3)))
            for child in group['children']:
                child['affine'] = (parent @ local_matrix(child['transform'], size) @ np.asarray(child.get('affine', np.eye(3)))).tolist()
                child['transform'] = v.transform({})
            siblings = parent_layers(frame, layer_id)
            at = siblings.index(group)
            siblings[at:at + 1] = group['children']
        return self.store.get(project_id)
