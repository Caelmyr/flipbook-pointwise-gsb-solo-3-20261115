import hashlib
import math
import numpy as np
from PIL import Image, ImageDraw
from . import validate as v
from .composite import render_frame, trim_bounds, difference, png_bytes
from .palettes import color_inventory


def frame_metrics(image):
    array = np.asarray(image.convert('RGBA'))
    alpha = array[:, :, 3]
    active = alpha > 0
    bounds = trim_bounds(image)
    if np.any(active):
        y, x = np.nonzero(active)
        weights = alpha[active].astype(float)
        center_x = float(np.sum(x * weights) / weights.sum())
        center_y = float(np.sum(y * weights) / weights.sum())
        mean = array[:, :, :3][active].mean(axis=0)
        visible_colors = len(np.unique(array[active], axis=0))
    else:
        center_x = center_y = 0
        mean = [0, 0, 0]
        visible_colors = 0
    partial = (alpha > 0) & (alpha < 255)
    return {
        'visible_pixels': int(active.sum()),
        'coverage_percent': round(float(active.mean()) * 100, 5),
        'partial_alpha_pixels': int(partial.sum()),
        'bounds': bounds,
        'alpha_centroid': [round(center_x, 5), round(center_y, 5)],
        'mean_rgb': [round(float(a), 4) for a in mean],
        'visible_colors': visible_colors,
        'touches_edge': bool(np.any(active[0]) or np.any(active[-1]) or np.any(active[:, 0]) or np.any(active[:, -1])),
        'empty': not bool(np.any(active)),
        'sha256': hashlib.sha256(image.tobytes()).hexdigest(),
        'png_bytes': len(png_bytes(image)),
    }


def animation_report(store, project):
    metrics = []
    duplicates = {}
    elapsed = 0
    previous = None
    centroids = []
    for index, frame in enumerate(project['frames']):
        image = render_frame(store, project, frame['id'])
        current = frame_metrics(image)
        current.update({'id': frame['id'], 'index': index, 'name': frame['name'], 'duration': frame['duration'], 'start': elapsed})
        duplicates.setdefault(current['sha256'], []).append(frame['id'])
        elapsed += frame['duration']
        if previous is not None:
            _, diff = difference(previous, image, gain=1)
            current['difference_from_previous'] = diff
        previous = image
        centroids.append(current['alpha_centroid'])
        metrics.append(current)
    jumps = []
    for index in range(1, len(centroids)):
        distance = math.dist(centroids[index - 1], centroids[index])
        jumps.append({'from': metrics[index - 1]['id'], 'to': metrics[index]['id'], 'distance_px': round(distance, 5), 'speed_px_per_second': round(distance / project['frames'][index - 1]['duration'] * 1000, 5)})
    warnings = []
    if any(m['empty'] for m in metrics):
        warnings.append('存在完全透明的空帧')
    if any(m['touches_edge'] for m in metrics):
        warnings.append('存在接触画布边界的内容，旋转或平移时可能被裁切')
    if any(m['partial_alpha_pixels'] for m in metrics):
        warnings.append('存在半透明像素；GIF导出会使用二值透明度')
    if len(set(frame['duration'] for frame in project['frames'])) > 1:
        warnings.append('使用了可变帧时长，导出GIF时会量化至10毫秒')
    return {
        'project_id': project['id'],
        'revision': project['revision'],
        'frame_count': len(metrics),
        'total_duration': elapsed,
        'average_fps': round(len(metrics) / elapsed * 1000, 4),
        'frames': metrics,
        'duplicate_groups': [ids for ids in duplicates.values() if len(ids) > 1],
        'motion': jumps,
        'warnings': warnings,
        'rendered_bytes_estimate': project['width'] * project['height'] * len(metrics) * 4,
    }


def alpha_collision(first, second, dx=0, dy=0, threshold=1):
    if first.size != second.size:
        raise v.Invalid('碰撞比较要求画布尺寸相同')
    shifted = Image.new('RGBA', first.size)
    shifted.alpha_composite(second, (dx, dy))
    a = np.asarray(first.getchannel('A')) >= threshold
    b = np.asarray(shifted.getchannel('A')) >= threshold
    overlap = a & b
    y, x = np.nonzero(overlap)
    bounds = [int(x.min()), int(y.min()), int(x.max()) + 1, int(y.max()) + 1] if len(x) else None
    overlay = np.asarray(first).copy()
    overlay[overlap] = (255, 85, 110, 255)
    return Image.fromarray(overlay), {
        'collision': bool(np.any(overlap)),
        'overlap_pixels': int(overlap.sum()),
        'overlap_bounds': bounds,
        'offset': [dx, dy],
    }


def seam_metrics(image):
    array = np.asarray(image.convert('RGBA'), dtype=float)
    horizontal = np.abs(array[:, 0] - array[:, -1])
    vertical = np.abs(array[0] - array[-1])
    return {
        'horizontal_mean_rgba_error': round(float(horizontal.mean()), 5),
        'vertical_mean_rgba_error': round(float(vertical.mean()), 5),
        'horizontal_mismatched_pixels': int(np.count_nonzero(np.any(horizontal > 0, axis=1))),
        'vertical_mismatched_pixels': int(np.count_nonzero(np.any(vertical > 0, axis=1))),
        'horizontal_exact_match': bool(np.all(horizontal == 0)),
        'vertical_exact_match': bool(np.all(vertical == 0)),
    }


def loop_seam(store, project):
    first = render_frame(store, project, project['frames'][0]['id'])
    last = render_frame(store, project, project['frames'][-1]['id'])
    image, details = difference(first, last)
    details['first_frame'] = project['frames'][0]['id']
    details['last_frame'] = project['frames'][-1]['id']
    details['note'] = '首尾不同并不一定表示循环错误，需结合运动方向与预期间隔判断'
    return image, details


def repeat_preview(image, columns=3, rows=3):
    columns = v.integer(columns, '重复列数', 1, 10)
    rows = v.integer(rows, '重复行数', 1, 10)
    if image.width * image.height * columns * rows > 16000000:
        raise v.Invalid('重复预览尺寸超限')
    output = Image.new('RGBA', (image.width * columns, image.height * rows))
    for y in range(rows):
        for x in range(columns):
            output.paste(image, (x * image.width, y * image.height))
    return output


def duplicate_cleanup(store, project_id, revision, adjacent_only=True):
    with store.edit(project_id, revision, '合并重复帧') as project:
        hashes = {}
        kept = []
        removed = {}
        previous_hash = None
        for frame in project['frames']:
            image = render_frame(store, project, frame['id'])
            digest = hashlib.sha256(image.tobytes()).hexdigest()
            target = kept[-1] if adjacent_only and digest == previous_hash and kept else None
            if not adjacent_only:
                target = hashes.get(digest)
            if target is not None and target['duration'] + frame['duration'] <= 10000:
                target['duration'] += frame['duration']
                removed[frame['id']] = target['id']
            else:
                kept.append(frame)
                hashes[digest] = frame
            previous_hash = digest
        project['frames'] = kept
        for clip in project['clips']:
            ids = [removed.get(key, key) for key in clip['frame_ids']]
            clip['frame_ids'] = list(dict.fromkeys(ids))
        for tilemap in project['tilemaps']:
            tilemap['tiles'] = [removed.get(key, key) for key in tilemap['tiles']]
        camera = project.get('camera')
        if camera and camera.get('source_frame') in removed:
            camera['source_frame'] = removed[camera['source_frame']]
    return store.get(project_id)



def motion_contact_sheet(store, project, columns=6, cell=120):
    columns = min(columns, len(project['frames']))
    rows = math.ceil(len(project['frames']) / columns)
    width, height = columns * cell, rows * (cell + 24)
    if width * height > 16000000:
        raise v.Invalid('联系表输出过大')
    result = Image.new('RGBA', (width, height), '#172633ff')
    draw = ImageDraw.Draw(result)
    from .drawing import font
    typeface = font(11)
    for index, frame in enumerate(project['frames']):
        image = render_frame(store, project, frame['id'])
        image.thumbnail((cell - 10, cell - 10), Image.Resampling.NEAREST)
        x = index % columns * cell + (cell - image.width) // 2
        y = index // columns * (cell + 24) + (cell - image.height) // 2
        result.alpha_composite(image, (x, y))
        draw.text((index % columns * cell + 5, index // columns * (cell + 24) + cell), f'{index + 1} / {frame["duration"]}ms', font=typeface, fill='white')
    return result
