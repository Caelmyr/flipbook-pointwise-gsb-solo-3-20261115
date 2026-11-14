import bisect
import math
from PIL import Image
from . import validate as v
from .model import find_frame, new_frame, new_id
from .composite import render_frame
from .tween import premultiplied_crossfade


def temporal_plan(project, body):
    fps = v.real(body.get('fps', 12), '目标帧率', .1, 50)
    mode = v.choice(body.get('mode', 'hold'), '时间重采样', ['hold', 'crossfade'])
    ids = v.frame_ids(body.get('frame_ids', [frame['id'] for frame in project['frames']]), project)
    frames = [find_frame(project, key) for key in ids]
    total = sum(frame['duration'] for frame in frames)
    count = max(1, int(round(total * fps / 1000)))
    if count > 300:
        raise v.Invalid('重采样后超过300帧')
    base, remainder = divmod(total, count)
    if base < 20 or base + bool(remainder) > 10000:
        raise v.Invalid('目标帧率无法满足20–10000毫秒帧时长限制')
    boundaries = [0]
    for frame in frames:
        boundaries.append(boundaries[-1] + frame['duration'])
    plan = []
    elapsed = 0
    wrap = v.boolean(body.get('wrap', False), '末帧向首帧过渡')
    for index in range(count):
        duration = base + (1 if index < remainder else 0)
        sample_time = min(total - 1e-6, elapsed + duration / 2)
        source_index = bisect.bisect_right(boundaries, sample_time) - 1
        source_index = min(source_index, len(frames) - 1)
        fraction = (sample_time - boundaries[source_index]) / frames[source_index]['duration']
        next_index = source_index + 1
        if next_index == len(frames):
            next_index = 0 if wrap else source_index
        plan.append({
            'index': index,
            'start': elapsed,
            'duration': duration,
            'sample_time': round(sample_time, 6),
            'first': frames[source_index]['id'],
            'second': frames[next_index]['id'],
            'factor': round(fraction, 6) if mode == 'crossfade' else 0,
        })
        elapsed += duration
    return {
        'source_frames': len(frames),
        'target_frames': count,
        'source_duration': total,
        'target_duration': elapsed,
        'actual_fps': round(count / total * 1000, 6),
        'mode': mode,
        'plan': plan,
    }


def resample_project(store, project_id, body):
    with store.lock:
        original = store.get(project_id)
        plan = temporal_plan(original, body)
        v.pixel_budget(original['width'], original['height'], plan['target_frames'])
        created = store.create(original['name'][:90] + ' · 时间重采样', original['width'], original['height'])
        cache = {}
        with store.edit(created['id'], 0, '重新采样动画时间') as project:
            project['frames'] = []
            project['palette'] = original['palette'][:]
            project['background'] = original['background']
            project['loop'] = original['loop']
            for item in plan['plan']:
                for frame_id in [item['first'], item['second']]:
                    if frame_id not in cache:
                        cache[frame_id] = render_frame(store, original, frame_id)
                first = cache[item['first']]
                second = cache[item['second']]
                factor = item['factor']
                image = premultiplied_crossfade(first, second, factor) if factor else first
                asset = store.put_image(created['id'], image)
                frame = new_frame(asset, f'采样 {item["index"] + 1}', item['duration'])
                source = find_frame(original, item['first'])
                frame['pivot'] = source['pivot'][:]
                frame['boxes'] = [dict(box) for box in source['boxes']]
                project['frames'].append(frame)
            project['clips'] = [{
                'id': new_id(),
                'name': '重采样循环',
                'frame_ids': [frame['id'] for frame in project['frames']],
                'direction': 'forward',
                'repeat': original['loop'],
            }]
            project['notes'] = f'来源项目{project_id}，修订{original["revision"]}；按时间中点采样，模式{plan["mode"]}，总时长保持{plan["target_duration"]}ms。'
        return {'project': store.get(created['id']), 'sampling': plan}


def exposure_chart(project, width=1000, height=260):
    from PIL import ImageDraw
    from .drawing import font
    total = sum(frame['duration'] for frame in project['frames'])
    image = Image.new('RGBA', (width, height), '#152635ff')
    painter = ImageDraw.Draw(image)
    typeface = font(12)
    left, right, top, bottom = 35, width - 25, 55, height - 45
    painter.text((left, 15), f'Exposure sheet / {len(project["frames"])} frames / {total}ms', font=font(18), fill='white')
    colors = ['#66e2bdff', '#8b9cffff', '#f8cc65ff', '#f58cabff']
    elapsed = 0
    for index, frame in enumerate(project['frames']):
        start = left + elapsed / total * (right - left)
        elapsed += frame['duration']
        end = left + elapsed / total * (right - left)
        row = index % 4
        y = top + row * 32
        painter.rectangle((start, y, max(start, end - 1), y + 25), fill=colors[row])
        if end - start > 25:
            painter.text((start + 2, y + 4), str(index + 1), font=typeface, fill='#152437ff')
    painter.line((left, bottom, right, bottom), fill='#9ab0c0ff')
    for tick in range(6):
        x = left + tick / 5 * (right - left)
        painter.line((x, bottom - 4, x, bottom + 4), fill='white')
        painter.text((x - 15, bottom + 10), f'{total * tick / 5:.0f}', font=typeface, fill='white')
    return image
