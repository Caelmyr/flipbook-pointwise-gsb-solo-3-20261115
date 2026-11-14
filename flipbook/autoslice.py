import numpy as np
from PIL import Image
from . import validate as v
from .collision import components
from .model import new_frame
from .importing import decode_sequence, fit_canvas


def occupied_runs(values, gap=0):
    spans = []
    begin = None
    last = None
    for index, occupied in enumerate(values):
        if occupied:
            if begin is None:
                begin = index
            last = index
        elif begin is not None and index - last > gap:
            spans.append((begin, last + 1))
            begin = last = None
    if begin is not None:
        spans.append((begin, last + 1))
    return spans


def gutter_rectangles(alpha, threshold=1, gap=0):
    occupied = alpha >= threshold
    rows = occupied_runs(np.any(occupied, axis=1), gap)
    boxes = []
    for top, bottom in rows:
        columns = occupied_runs(np.any(occupied[top:bottom], axis=0), gap)
        for left, right in columns:
            block = occupied[top:bottom, left:right]
            y, x = np.nonzero(block)
            if not len(x):
                continue
            boxes.append({'x': left + int(x.min()), 'y': top + int(y.min()), 'width': int(x.max() - x.min() + 1), 'height': int(y.max() - y.min() + 1), 'area': int(len(x))})
    return boxes


def detect_sprites(image, body):
    threshold = v.integer(body.get('threshold', 1), '透明度阈值', 1, 255)
    minimum = v.integer(body.get('minimum', 4), '最小精灵像素', 1, 1000000)
    mode = v.choice(body.get('mode', 'gutters'), '自动切片方式', ['gutters', 'components'])
    padding = v.integer(body.get('padding', 0), '切片留白', 0, 32)
    gap = v.integer(body.get('gap', 0), '允许内部透明间隔', 0, 32)
    alpha = np.asarray(image.convert('RGBA'))[:, :, 3]
    if mode == 'components':
        if alpha.size > 1000000:
            raise v.Invalid('连通精灵自动切片限100万像素')
        boxes = components(alpha >= threshold, minimum)
    else:
        boxes = gutter_rectangles(alpha, threshold, gap)
        boxes = [box for box in boxes if box['area'] >= minimum]
    if not boxes:
        raise v.Invalid('没有检测到非透明精灵')
    if len(boxes) > 300:
        raise v.Invalid('切片超过300个，请增大最小像素或合并透明间隔')
    result = []
    for box in sorted(boxes, key=lambda box: (box['y'], box['x'])):
        left = max(0, box['x'] - padding)
        top = max(0, box['y'] - padding)
        right = min(image.width, box['x'] + box['width'] + padding)
        bottom = min(image.height, box['y'] + box['height'] + padding)
        result.append({'x': left, 'y': top, 'width': right - left, 'height': bottom - top, 'source_area': box['area']})
    return result


def create_from_sprites(store, stream, filename, body):
    image = decode_sequence(stream, 1)[0]['image']
    boxes = detect_sprites(image, body)
    minimum_w = max(box['width'] for box in boxes)
    minimum_h = max(box['height'] for box in boxes)
    width = v.integer(body.get('width', max(8, minimum_w)), '统一画布宽', 8, 2048)
    height = v.integer(body.get('height', max(8, minimum_h)), '统一画布高', 8, 2048)
    v.pixel_budget(width, height, len(boxes))
    if width < minimum_w or height < minimum_h:
        raise v.Invalid('统一画布不足以容纳检测到的精灵')
    project = store.create(v.text(filename, '文件名', 180)[:90] + ' · 自动切片', width, height)
    with store.edit(project['id'], 0, '从透明图自动拆分精灵') as working:
        working['frames'] = []
        for index, box in enumerate(boxes):
            x, y, w, h = [box[key] for key in ['x', 'y', 'width', 'height']]
            sprite = image.crop((x, y, x + w, y + h))
            sprite = fit_canvas(sprite, (width, height), 'center')
            frame = new_frame(store.put_image(project['id'], sprite), f'精灵 {index + 1}')
            frame['notes'] = f'原图区域：{x},{y},{w},{h}'
            working['frames'].append(frame)
        working['notes'] = '自动切片依赖透明沟槽或连通组件；不相连的精灵部件可能被拆开，请核对后使用。'
    return {'project': store.get(project['id']), 'regions': boxes}
