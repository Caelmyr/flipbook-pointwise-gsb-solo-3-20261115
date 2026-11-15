import hashlib
import json
import math
import numpy as np
from PIL import Image, ImageDraw
from . import validate as v
from .composite import render_frame, trim_bounds, difference, png_bytes
from .exposures import clip_exposures
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


def cleanup_scan(store, project, adjacent_only=True):
    frames = []
    for index, frame in enumerate(project['frames']):
        image = render_frame(store, project, frame['id'])
        frames.append({
            'id': frame['id'],
            'name': frame['name'],
            'index': index,
            'duration': frame['duration'],
            'digest': hashlib.sha256(image.tobytes()).hexdigest(),
            'boxes': frame.get('boxes', []),
            'pivot': frame.get('pivot', [0.5, 0.5]),
        })
    for info in frames:
        info['signature'] = json.dumps([info['digest'], info['boxes'], info['pivot']],
                                       ensure_ascii=False, sort_keys=True)
    kept = []
    removed = {}
    outcomes = {}
    targets = {}
    siblings = {}
    previous = None
    for info in frames:
        target = None
        if adjacent_only:
            if kept and previous is not None and previous['signature'] == info['signature']:
                target = kept[-1]
        else:
            target = targets.get(info['signature'])
        if target is not None and target['duration'] + info['duration'] <= 10000:
            target['duration'] += info['duration']
            target['absorbed'].append(info)
            removed[info['id']] = target['id']
            outcomes[info['id']] = ('merged', target)
            previous = info
            continue
        entry = dict(info, absorbed=[])
        kept.append(entry)
        if not adjacent_only:
            targets[info['signature']] = entry
        if target is not None:
            outcomes[info['id']] = ('duration', target)
        else:
            sibling = None
            if adjacent_only:
                if previous is not None and previous['digest'] == info['digest']:
                    sibling = previous
            else:
                sibling = siblings.get(info['digest'])
            if sibling is not None:
                outcomes[info['id']] = ('metadata', sibling)
        siblings.setdefault(info['digest'], entry)
        previous = info
    return {'frames': frames, 'kept': kept, 'removed': removed, 'outcomes': outcomes}


def playback_sequence(clip, exposures, digests):
    items = [(digests[item['frame_id']], item['duration']) for item in exposures]
    if clip['direction'] == 'reverse':
        items = items[::-1]
    elif clip['direction'] == 'pingpong':
        items = items + items[-2:0:-1]
    return items


def cleanup_preview(store, project, adjacent_only=True, scan=None):
    scan = scan or cleanup_scan(store, project, adjacent_only)
    removed = scan['removed']
    changed = set(removed) | {entry['id'] for entry in scan['kept'] if entry['absorbed']}
    digests = {info['id']: info['digest'] for info in scan['frames']}
    mergeable = []
    for entry in scan['kept']:
        if entry['absorbed']:
            mergeable.append({
                'keep': {'id': entry['id'], 'name': entry['name'], 'index': entry['index']},
                'absorbed': [{'id': item['id'], 'name': item['name'], 'index': item['index'],
                              'duration': item['duration']} for item in entry['absorbed']],
                'result_duration': entry['duration'],
            })
    unmergeable = []
    for info in scan['frames']:
        outcome = scan['outcomes'].get(info['id'])
        if outcome is None or outcome[0] == 'merged':
            continue
        kind, other = outcome
        if kind == 'duration':
            reason = f'画面、碰撞框与枢轴均与「{other["name"]}」一致，但累加时长会超过单帧10000ms上限，两帧都保留'
        else:
            differ = []
            if info['boxes'] != other['boxes']:
                differ.append('碰撞框')
            if info['pivot'] != other['pivot']:
                differ.append('枢轴')
            reason = f'画面与「{other["name"]}」逐像素相同，但{"、".join(differ)}不同，合并会丢失动作含义，两帧都保留'
        unmergeable.append({'id': info['id'], 'name': info['name'], 'index': info['index'], 'reason': reason})
    clips = []
    for clip in project.get('clips', []):
        exposures = clip_exposures(project, clip)
        affected = any(item['frame_id'] in changed for item in exposures)
        before = playback_sequence(clip, exposures, digests)
        moved = [{'frame_id': removed.get(item['frame_id'], item['frame_id']), 'duration': item['duration']}
                 for item in exposures]
        after = playback_sequence(clip, moved, digests)
        clips.append({
            'id': clip['id'],
            'name': clip['name'],
            'affected': affected,
            'before_duration': sum(duration for _, duration in before),
            'after_duration': sum(duration for _, duration in after),
            'timing_preserved': before == after,
        })
    tilemaps = []
    for tilemap in project.get('tilemaps', []):
        remapped = sum(1 for key in tilemap['tiles'] if key in removed)
        if remapped:
            tilemaps.append({'id': tilemap['id'], 'name': tilemap['name'], 'remapped_slots': remapped,
                             'note': '瓦片槽位数量、顺序与图层翻转标记保持不变'})
    return {
        'adjacent_only': adjacent_only,
        'before': {'frames': len(scan['frames']),
                   'total_duration': sum(info['duration'] for info in scan['frames'])},
        'after': {'frames': len(scan['kept']),
                  'total_duration': sum(entry['duration'] for entry in scan['kept'])},
        'mergeable': mergeable,
        'unmergeable': unmergeable,
        'clips': clips,
        'tilemaps': tilemaps,
        'note': '只合并画面逐像素相同、碰撞框与枢轴一致的帧，时长累加到保留帧，时间轴总时长不变；'
                '被动作片段或地图引用的重复画面照常整理，各片段保留自己的曝光时序与往返端点。',
    }


def duplicate_cleanup(store, project_id, revision, adjacent_only=True):
    with store.edit(project_id, revision, '合并重复帧') as project:
        scan = cleanup_scan(store, project, adjacent_only)
        removed = scan['removed']
        if not removed:
            raise v.Invalid('没有可安全合并的重复画面')
        report = cleanup_preview(store, project, adjacent_only, scan)
        changed = set(removed) | {entry['id'] for entry in scan['kept'] if entry['absorbed']}
        durations = {frame['id']: frame['duration'] for frame in project['frames']}
        for clip in project['clips']:
            exposures = clip.get('exposures')
            if exposures is None:
                exposures = [{'frame_id': key, 'duration': durations[key]} for key in clip['frame_ids']]
            if not any(item['frame_id'] in changed for item in exposures):
                continue
            clip['exposures'] = [{'frame_id': removed.get(item['frame_id'], item['frame_id']),
                                  'duration': item['duration']} for item in exposures]
            clip['frame_ids'] = list(dict.fromkeys(removed.get(key, key) for key in clip['frame_ids']))
        frames = {frame['id']: frame for frame in project['frames']}
        project['frames'] = [frames[entry['id']] for entry in scan['kept']]
        for entry in scan['kept']:
            frames[entry['id']]['duration'] = entry['duration']
        for tilemap in project['tilemaps']:
            tilemap['tiles'] = [removed.get(key, key) for key in tilemap['tiles']]
        for workspace in project.get('atlas_workspaces', []):
            workspace['locked'] = list(dict.fromkeys(removed.get(key, key)
                                                     for key in workspace.get('locked', [])))
    return {'project': store.get(project_id), 'cleanup': report}



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
