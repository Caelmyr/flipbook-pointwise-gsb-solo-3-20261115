from collections import deque
import numpy as np
from PIL import Image, ImageDraw
from . import validate as v
from .model import find_frame
from .composite import render_frame


def components(binary, minimum=1):
    height, width = binary.shape
    visited = np.zeros(binary.shape, dtype=bool)
    boxes = []
    for y in range(height):
        for x in range(width):
            if visited[y, x] or not binary[y, x]:
                continue
            queue = deque([(x, y)])
            visited[y, x] = True
            left = right = x
            top = bottom = y
            area = 0
            while queue:
                px, py = queue.popleft()
                area += 1
                left, right = min(left, px), max(right, px)
                top, bottom = min(top, py), max(bottom, py)
                for nx, ny in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
                    if not 0 <= nx < width or not 0 <= ny < height:
                        continue
                    if visited[ny, nx] or not binary[ny, nx]:
                        continue
                    visited[ny, nx] = True
                    queue.append((nx, ny))
            if area >= minimum:
                boxes.append({'x': left, 'y': top, 'width': right - left + 1, 'height': bottom - top + 1, 'area': area})
            if len(boxes) > 1000:
                raise v.Invalid('透明对象过多，不能生成碰撞框')
    return boxes


def occupied_grid(image, cell_size=8, threshold=1, occupancy=.1):
    alpha = np.asarray(image.convert('RGBA'))[:, :, 3]
    rows = []
    for y in range(0, image.height, cell_size):
        for x in range(0, image.width, cell_size):
            block = alpha[y:y + cell_size, x:x + cell_size] >= threshold
            ratio = float(block.mean())
            if ratio >= occupancy:
                rows.append({'x': x, 'y': y, 'width': min(cell_size, image.width - x), 'height': min(cell_size, image.height - y), 'coverage': round(ratio, 5)})
    return rows


def auto_collision_boxes(store, project_id, revision, body):
    threshold = v.integer(body.get('threshold', 128), '透明度阈值', 1, 255)
    minimum = v.integer(body.get('minimum', 4), '最小组件像素', 1, 1000000)
    mode = v.choice(body.get('mode', 'union'), '碰撞框方式', ['union', 'components'])
    padding = v.integer(body.get('padding', 0), '碰撞框扩边', 0, 64)
    with store.edit(project_id, revision, '自动生成碰撞框') as project:
        ids = v.frame_ids(body.get('frame_ids'), project)
        for frame_id in ids:
            frame = find_frame(project, frame_id)
            image = render_frame(store, project, frame_id)
            alpha = np.asarray(image.getchannel('A')) >= threshold
            if mode == 'components':
                if alpha.size > 1000000:
                    raise v.Invalid('组件碰撞框限100万像素')
                boxes = components(alpha, minimum)
            else:
                y, x = np.nonzero(alpha)
                boxes = [{'x': int(x.min()), 'y': int(y.min()), 'width': int(x.max() - x.min() + 1), 'height': int(y.max() - y.min() + 1)}] if len(x) else []
            if len(boxes) > 30:
                raise v.Invalid('碰撞框超过30个，请提高最小组件面积')
            output = []
            for index, box in enumerate(boxes):
                left = max(0, box['x'] - padding)
                top = max(0, box['y'] - padding)
                right = min(project['width'], box['x'] + box['width'] + padding)
                bottom = min(project['height'], box['y'] + box['height'] + padding)
                output.append({'name': f'碰撞 {index + 1}', 'x': left, 'y': top, 'width': right - left, 'height': bottom - top})
            frame['boxes'] = output
    return store.get(project_id)


def collision_preview(store, project, frame_id):
    frame = find_frame(project, frame_id)
    image = render_frame(store, project, frame_id)
    drawing = ImageDraw.Draw(image)
    for box in frame['boxes']:
        drawing.rectangle((box['x'], box['y'], box['x'] + box['width'] - 1, box['y'] + box['height'] - 1), outline='#ff667eff', width=max(1, project['width'] // 200))
    x = frame['pivot'][0] * project['width']
    y = frame['pivot'][1] * project['height']
    drawing.line((x - 4, y, x + 4, y), fill='#f8cc65ff', width=1)
    drawing.line((x, y - 4, x, y + 4), fill='#f8cc65ff', width=1)
    return image


def metadata_sequence(project):
    elapsed = 0
    result = []
    for frame in project['frames']:
        boxes = []
        for box in frame['boxes']:
            boxes.append({
                'name': box['name'],
                'left': box['x'] / project['width'],
                'top': box['y'] / project['height'],
                'right': (box['x'] + box['width']) / project['width'],
                'bottom': (box['y'] + box['height']) / project['height'],
            })
        result.append({'id': frame['id'], 'name': frame['name'], 'start_ms': elapsed, 'duration_ms': frame['duration'], 'pivot': frame['pivot'], 'normalized_collision_boxes': boxes})
        elapsed += frame['duration']
    return {'canvas': [project['width'], project['height']], 'duration': elapsed, 'frames': result}
