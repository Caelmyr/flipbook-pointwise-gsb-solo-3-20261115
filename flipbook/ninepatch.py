import numpy as np
from PIL import Image
from . import validate as v
from .model import find_frame, new_frame
from .composite import render_frame


def validate_margins(size, body):
    left = v.integer(body.get('left', 8), '左边框', 0, size[0] - 1)
    right = v.integer(body.get('right', 8), '右边框', 0, size[0] - 1)
    top = v.integer(body.get('top', 8), '上边框', 0, size[1] - 1)
    bottom = v.integer(body.get('bottom', 8), '下边框', 0, size[1] - 1)
    if left + right >= size[0] or top + bottom >= size[1]:
        raise v.Invalid('边框总和必须小于图像尺寸')
    return left, right, top, bottom


def tiled_resize(image, size, tile_x, tile_y):
    if not tile_x and not tile_y:
        return image.resize(size, Image.Resampling.NEAREST)
    if tile_x and tile_y:
        source = image
    elif tile_x:
        source = image.resize((image.width, size[1]), Image.Resampling.NEAREST)
    else:
        source = image.resize((size[0], image.height), Image.Resampling.NEAREST)
    result = Image.new('RGBA', size)
    for y in range(0, size[1], max(1, source.height)):
        for x in range(0, size[0], max(1, source.width)):
            result.alpha_composite(source, (x, y))
    return result


def nine_slice(image, target_size, margins, tile_center=False):
    left, right, top, bottom = margins
    width, height = target_size
    if width < left + right + 1 or height < top + bottom + 1:
        raise v.Invalid('目标尺寸不足以保留边框')
    source_x = [0, left, image.width - right, image.width]
    source_y = [0, top, image.height - bottom, image.height]
    target_x = [0, left, width - right, width]
    target_y = [0, top, height - bottom, height]
    output = Image.new('RGBA', target_size)
    for row in range(3):
        for column in range(3):
            source = (source_x[column], source_y[row], source_x[column + 1], source_y[row + 1])
            destination = (target_x[column], target_y[row], target_x[column + 1], target_y[row + 1])
            w = destination[2] - destination[0]
            h = destination[3] - destination[1]
            if w <= 0 or h <= 0 or source[2] <= source[0] or source[3] <= source[1]:
                continue
            piece = image.crop(source)
            piece = tiled_resize(piece, (w, h), tile_center and column == 1, tile_center and row == 1)
            output.paste(piece, destination[:2])
    return output


def ninepatch_project(store, project_id, body):
    source = store.get(project_id)
    frame_id = body.get('frame_id', source['frames'][0]['id'])
    frame = find_frame(source, frame_id)
    image = render_frame(store, source, frame_id)
    margins = validate_margins(image.size, body)
    width, height = v.dimensions(body)
    tiled = v.boolean(body.get('tiled', False), '平铺中心')
    output = nine_slice(image, (width, height), margins, tiled)
    created = store.create(source['name'][:90] + ' · 九宫格', width, height)
    with store.edit(created['id'], 0, '九宫格缩放生成') as project:
        asset = store.put_image(created['id'], output)
        project['frames'] = [new_frame(asset, frame['name'], frame['duration'])]
        project['palette'] = source['palette'][:]
        project['notes'] = f'九宫格来源：{source["id"]} / {frame_id}，边框{margins}，平铺{tiled}'
    return store.get(created['id'])


def seam_blend(image, overlap=4, axes='both'):
    array = np.asarray(image.convert('RGBA'), dtype=float).copy()
    if overlap * 2 >= min(image.size):
        raise v.Invalid('融合带不能超过图像短边的一半')
    if axes in ('horizontal', 'both'):
        for distance in range(overlap):
            weight = .5 * (1 - distance / overlap)
            left = array[:, distance].copy()
            right = array[:, -1 - distance].copy()
            array[:, distance] = left * (1 - weight) + right * weight
            array[:, -1 - distance] = right * (1 - weight) + left * weight
    if axes in ('vertical', 'both'):
        for distance in range(overlap):
            weight = .5 * (1 - distance / overlap)
            top = array[distance].copy()
            bottom = array[-1 - distance].copy()
            array[distance] = top * (1 - weight) + bottom * weight
            array[-1 - distance] = bottom * (1 - weight) + top * weight
    return Image.fromarray(np.rint(array).astype(np.uint8))


def seamless_frames(store, project_id, revision, body):
    overlap = v.integer(body.get('overlap', 4), '边缘融合带', 1, 64)
    axes = v.choice(body.get('axes', 'both'), '融合方向', ['horizontal', 'vertical', 'both'])
    with store.edit(project_id, revision, '瓦片边缘融合') as project:
        ids = v.frame_ids(body.get('frame_ids'), project)
        for frame_id in ids:
            frame = find_frame(project, frame_id)
            from .layer_nodes import walk_layers
            for layer in walk_layers(frame['layers']):
                if layer.get('kind') == 'group':
                    continue
                if layer['locked']:
                    raise v.Invalid('边缘融合包含锁定图层')
                if layer['transform'] != v.transform({}):
                    raise v.Invalid('请先烘焙图层变换')
                from .role_tools import require_ordinary
                require_ordinary(layer)
                from .cels import require_detached
                require_detached(layer)
                image = seam_blend(store.image(project_id, layer['asset']), overlap, axes)
                layer['asset'] = store.put_image(project_id, image)
    return store.get(project_id)
