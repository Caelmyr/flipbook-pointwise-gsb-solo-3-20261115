from PIL import Image, ImageOps
from . import validate as v
from .model import find_frame
from .composite import opaque_bounds_union, render_frame


ANCHORS = {
    'top_left': (0, 0),
    'top': (.5, 0),
    'top_right': (1, 0),
    'left': (0, .5),
    'center': (.5, .5),
    'right': (1, .5),
    'bottom_left': (0, 1),
    'bottom': (.5, 1),
    'bottom_right': (1, 1),
}


def require_unlocked(project):
    from .role_tools import require_ordinary
    from .layer_nodes import walk_layers
    for frame in project['frames']:
        for layer in walk_layers(frame['layers']):
            require_ordinary(layer)
    from .cels import require_detached
    for frame in project['frames']:
        for layer in walk_layers(frame['layers']):
            require_detached(layer)
    if any(layer['locked'] for frame in project['frames'] for layer in walk_layers(frame['layers'])):
        raise v.Invalid('几何处理包含锁定图层，请先解锁')


def expand_groups(store, project):
    """Flatten every group into its children before whole-canvas geometry.

    Canvas-wide resampling needs every pixel in one coordinate space.  Folding
    the group matrices into the children first keeps the exact rendered result
    and leaves normal leaf layers that the existing bake step can handle.
    """
    from .layer_nodes import flatten_to_children
    from .layer_tree import passthrough_group
    size = (project['width'], project['height'])

    def walk(items, depth=0):
        if depth > 8:
            raise v.Invalid('图层组合过深')
        result = []
        for node in items:
            if node.get('kind') == 'group':
                if not passthrough_group(node):
                    raise v.Invalid('组合整体透明度、混合模式或遮罩不为默认；请先解组或恢复后再做画布几何操作')
                result.extend(walk(flatten_to_children(node, size), depth + 1))
            else:
                result.append(node)
        return result

    for frame in project['frames']:
        frame['layers'] = walk(frame['layers'])


def bake_all(store, project):
    from .composite import transform_world
    from .layer_tree import node_matrix
    size = (project['width'], project['height'])
    for frame in project['frames']:
        for layer in frame['layers']:
            if layer['transform'] == v.transform({}) and 'affine' not in layer:
                continue
            image = transform_world(store.image(project['id'], layer['asset']), node_matrix(layer, size), size)
            layer['asset'] = store.put_image(project['id'], image)
            layer['transform'] = v.transform({})
            layer.pop('affine', None)
            layer.pop('cel_id', None)


def translate_boxes(frame, dx, dy, width, height):
    result = []
    for box in frame['boxes']:
        left = max(0, box['x'] + dx)
        top = max(0, box['y'] + dy)
        right = min(width, box['x'] + dx + box['width'])
        bottom = min(height, box['y'] + dy + box['height'])
        if left < right and top < bottom:
            result.append({'x': int(left), 'y': int(top), 'width': int(right - left), 'height': int(bottom - top), 'name': box['name']})
    frame['boxes'] = result


def resize_canvas(store, project_id, revision, body):
    width, height = v.dimensions(body)
    mode = v.choice(body.get('mode', 'canvas'), '尺寸模式', ['canvas', 'scale'])
    anchor = v.choice(body.get('anchor', 'center'), '锚点', ANCHORS)
    interpolation = v.choice(body.get('interpolation', 'nearest'), '插值', ['nearest', 'lanczos'])
    with store.edit(project_id, revision, '画布尺寸调整') as project:
        require_unlocked(project)
        expand_groups(store, project)
        bake_all(store, project)
        old_w, old_h = project['width'], project['height']
        v.pixel_budget(width, height, sum(len(f['layers']) for f in project['frames']))
        ax, ay = ANCHORS[anchor]
        dx, dy = int(round((width - old_w) * ax)), int(round((height - old_h) * ay))
        for frame in project['frames']:
            for layer in frame['layers']:
                image = store.image(project_id, layer['asset'])
                if mode == 'scale':
                    method = Image.Resampling.NEAREST if interpolation == 'nearest' else Image.Resampling.LANCZOS
                    image = image.resize((width, height), method)
                else:
                    result = Image.new('RGBA', (width, height))
                    result.alpha_composite(image, (dx, dy))
                    image = result
                layer['asset'] = store.put_image(project_id, image)
            if mode == 'scale':
                for box in frame['boxes']:
                    box['x'] = int(round(box['x'] * width / old_w))
                    box['y'] = int(round(box['y'] * height / old_h))
                    box['width'] = max(1, int(round(box['width'] * width / old_w)))
                    box['height'] = max(1, int(round(box['height'] * height / old_h)))
                    box['width'] = min(box['width'], width - box['x'])
                    box['height'] = min(box['height'], height - box['y'])
            else:
                frame['pivot'] = [max(0, min(1, (frame['pivot'][0] * old_w + dx) / width)), max(0, min(1, (frame['pivot'][1] * old_h + dy) / height))]
                translate_boxes(frame, dx, dy, width, height)
        project['width'], project['height'] = width, height
        project['tilemaps'] = []
    return store.get(project_id)


def crop_all(store, project_id, revision, body):
    with store.edit(project_id, revision, '统一裁切动画画布') as project:
        require_unlocked(project)
        expand_groups(store, project)
        bake_all(store, project)
        rectangle = v.rect(body, project['width'], project['height'])
        x, y, width, height = [rectangle[key] for key in ['x', 'y', 'width', 'height']]
        if min(width, height) < 8:
            raise v.Invalid('裁切后画布至少8×8')
        old_w, old_h = project['width'], project['height']
        for frame in project['frames']:
            for layer in frame['layers']:
                image = store.image(project_id, layer['asset']).crop((x, y, x + width, y + height))
                layer['asset'] = store.put_image(project_id, image)
            frame['pivot'] = [max(0, min(1, (frame['pivot'][0] * old_w - x) / width)), max(0, min(1, (frame['pivot'][1] * old_h - y) / height))]
            translate_boxes(frame, -x, -y, width, height)
        project['width'], project['height'] = width, height
        project['tilemaps'] = []
    return store.get(project_id)


def trim_all(store, project_id, revision, padding=0):
    padding = v.integer(padding, '裁边留白', 0, 64)
    with store.lock:
        project = store.get(project_id)
        images = [render_frame(store, project, frame['id']) for frame in project['frames']]
        bounds = opaque_bounds_union(images)
        if bounds is None:
            raise v.Invalid('全部帧为空，不能自动裁边')
        x = max(0, bounds[0] - padding)
        y = max(0, bounds[1] - padding)
        right = min(project['width'], bounds[2] + padding)
        bottom = min(project['height'], bounds[3] + padding)
        if right - x < 8:
            x = min(x, project['width'] - 8)
            right = min(project['width'], x + 8)
        if bottom - y < 8:
            y = min(y, project['height'] - 8)
            bottom = min(project['height'], y + 8)
        return crop_all(store, project_id, revision, {'x': x, 'y': y, 'width': right - x, 'height': bottom - y})


def align_content(store, project_id, revision, ids, anchor='center'):
    v.choice(anchor, '对齐位置', ANCHORS)
    with store.edit(project_id, revision, '按可见内容对齐') as project:
        ids = v.frame_ids(ids, project)
        require_unlocked(project)
        expand_groups(store, project)
        bake_all(store, project)
        for frame_id in ids:
            frame = find_frame(project, frame_id)
            image = render_frame(store, project, frame_id)
            bounds = image.getchannel('A').getbbox()
            if bounds is None:
                continue
            ax, ay = ANCHORS[anchor]
            target_x = (project['width'] - (bounds[2] - bounds[0])) * ax
            target_y = (project['height'] - (bounds[3] - bounds[1])) * ay
            dx, dy = int(round(target_x - bounds[0])), int(round(target_y - bounds[1]))
            for layer in frame['layers']:
                current = store.image(project_id, layer['asset'])
                shifted = Image.new('RGBA', current.size)
                shifted.alpha_composite(current, (dx, dy))
                layer['asset'] = store.put_image(project_id, shifted)
            translate_boxes(frame, dx, dy, project['width'], project['height'])
    return store.get(project_id)


def rotate_canvas(store, project_id, revision, quarter_turns=1):
    turns = v.integer(quarter_turns, '顺时针90°次数', 1, 3)
    operation = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}[turns]
    with store.edit(project_id, revision, '旋转动画画布') as project:
        require_unlocked(project)
        expand_groups(store, project)
        bake_all(store, project)
        for frame in project['frames']:
            for layer in frame['layers']:
                image = store.image(project_id, layer['asset']).transpose(operation)
                layer['asset'] = store.put_image(project_id, image)
            x, y = frame['pivot']
            frame['pivot'] = [1 - y, x] if turns == 1 else [1 - x, 1 - y] if turns == 2 else [y, 1 - x]
            frame['boxes'] = []
        if turns % 2:
            project['width'], project['height'] = project['height'], project['width']
        project['tilemaps'] = []
    return store.get(project_id)
