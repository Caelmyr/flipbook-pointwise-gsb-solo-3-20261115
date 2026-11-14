import math
from copy import deepcopy
import numpy as np
from PIL import Image
from . import validate as v
from .model import find_frame, find_layer, clone_frame, new_frame, new_layer
from .composite import render_frame, transform_layer


def ease(t, mode='linear', steps=5):
    t = max(0, min(1, t))
    if mode == 'linear':
        return t
    if mode == 'ease_in':
        return t * t
    if mode == 'ease_out':
        return 1 - (1 - t) ** 2
    if mode == 'ease_in_out':
        return 2 * t * t if t < .5 else 1 - (-2 * t + 2) ** 2 / 2
    if mode == 'smoothstep':
        return t * t * (3 - 2 * t)
    if mode == 'steps':
        return math.floor(t * steps) / steps if t < 1 else 1
    if mode == 'bounce':
        n, d = 7.5625, 2.75
        if t < 1 / d:
            return n * t * t
        if t < 2 / d:
            t -= 1.5 / d
            return n * t * t + .75
        if t < 2.5 / d:
            t -= 2.25 / d
            return n * t * t + .9375
        t -= 2.625 / d
        return n * t * t + .984375
    if mode == 'elastic':
        if t in (0, 1):
            return t
        return 2 ** (-10 * t) * math.sin((t * 10 - .75) * 2 * math.pi / 3) + 1
    raise v.Invalid('缓动方式不支持')


def lerp(first, second, factor):
    return first + (second - first) * factor


def shortest_rotation(first, second, factor):
    difference = (second - first + 180) % 360 - 180
    return first + difference * factor


def interpolate_transform(first, second, factor, shortest=True):
    result = {}
    for name in ['x', 'y', 'scale_x', 'scale_y']:
        value = lerp(first[name], second[name], factor)
        result[name] = int(round(value)) if name in ('x', 'y') else max(.01, value)
    result['rotation'] = shortest_rotation(first['rotation'], second['rotation'], factor) if shortest else lerp(first['rotation'], second['rotation'], factor)
    result['flip_x'] = first['flip_x'] if factor < .5 else second['flip_x']
    result['flip_y'] = first['flip_y'] if factor < .5 else second['flip_y']
    return result


def premultiplied_crossfade(first, second, factor):
    a = np.asarray(first.convert('RGBA'), dtype=float) / 255
    b = np.asarray(second.convert('RGBA'), dtype=float) / 255
    alpha = a[:, :, 3:4] * (1 - factor) + b[:, :, 3:4] * factor
    premultiplied = a[:, :, :3] * a[:, :, 3:4] * (1 - factor) + b[:, :, :3] * b[:, :, 3:4] * factor
    rgb = np.divide(premultiplied, alpha, out=np.zeros_like(premultiplied), where=alpha > 1e-9)
    return Image.fromarray(np.rint(np.clip(np.concatenate([rgb, alpha], axis=2), 0, 1) * 255).astype(np.uint8))


def bezier_position(points, t):
    values = np.asarray(points, dtype=float)
    while len(values) > 1:
        values = values[:-1] * (1 - t) + values[1:] * t
    return values[0].tolist()


class Tween:
    def __init__(self, store):
        self.store = store

    def transform(self, project_id, revision, body):
        count = v.integer(body.get('count', 8), '补间帧数', 1, 60)
        mode = v.easing(body.get('easing', 'smoothstep'))
        duration = v.duration(body.get('duration', 80))
        target = v.transform(body.get('target', {}))
        end_opacity = v.real(body.get('opacity', 1), '末端透明度', 0, 1)
        with self.store.edit(project_id, revision, '图层变换补间') as project:
            frame, layer = find_layer(project, body.get('frame_id'), body.get('layer_id'))
            if layer['locked']:
                raise v.Invalid('请先解锁补间图层')
            if len(project['frames']) + count > 300:
                raise v.Invalid('补间后超过300帧')
            source_transform = deepcopy(layer['transform'])
            source_opacity = layer['opacity']
            layer_index = frame['layers'].index(layer)
            additions = []
            for i in range(1, count + 1):
                factor = ease(i / count, mode)
                copied = clone_frame(frame)
                copied['name'] = f'补间 {i}/{count}'
                copied['duration'] = duration
                target_layer = copied['layers'][layer_index]
                target_layer['transform'] = interpolate_transform(source_transform, target, factor)
                target_layer['opacity'] = max(0, min(1, lerp(source_opacity, end_opacity, factor)))
                additions.append(copied)
            index = project['frames'].index(frame)
            project['frames'][index + 1:index + 1] = additions
        return self.store.get(project_id)

    def crossfade(self, project_id, revision, body):
        count = v.integer(body.get('count', 6), '过渡帧数', 1, 60)
        mode = v.easing(body.get('easing', 'linear'))
        duration = v.duration(body.get('duration', 80))
        with self.store.edit(project_id, revision, '透明度交叉过渡') as project:
            first = find_frame(project, body.get('first'))
            second = find_frame(project, body.get('second'))
            if first['id'] == second['id']:
                raise v.Invalid('过渡需要两个不同帧')
            if len(project['frames']) + count > 300:
                raise v.Invalid('过渡后超过300帧')
            a = render_frame(self.store, project, first['id'])
            b = render_frame(self.store, project, second['id'])
            additions = []
            for index in range(1, count + 1):
                factor = ease(index / (count + 1), mode)
                image = premultiplied_crossfade(a, b, factor)
                additions.append(new_frame(self.store.put_image(project_id, image), f'过渡 {index}', duration))
            insertion = project['frames'].index(first) + 1
            project['frames'][insertion:insertion] = additions
        return self.store.get(project_id)

    def path(self, project_id, revision, body):
        count = v.integer(body.get('count', 12), '路径帧数', 2, 60)
        mode = v.easing(body.get('easing', 'linear'))
        duration = v.duration(body.get('duration', 80))
        with self.store.edit(project_id, revision, '贝塞尔路径动画') as project:
            frame, layer = find_layer(project, body.get('frame_id'), body.get('layer_id'))
            if layer['locked']:
                raise v.Invalid('路径图层已锁定')
            control = v.points(body.get('points'), project['width'], project['height'], 2, 8)
            if len(project['frames']) + count > 300:
                raise v.Invalid('路径动画超过300帧')
            index = frame['layers'].index(layer)
            origin = control[0]
            additions = []
            for i in range(1, count + 1):
                t = ease(i / count, mode)
                position = bezier_position(control, t)
                copied = clone_frame(frame)
                copied['name'] = f'路径 {i}'
                copied['duration'] = duration
                transform = copied['layers'][index]['transform']
                transform['x'] = int(round(layer['transform']['x'] + position[0] - origin[0]))
                transform['y'] = int(round(layer['transform']['y'] + position[1] - origin[1]))
                additions.append(copied)
            insertion = project['frames'].index(frame) + 1
            project['frames'][insertion:insertion] = additions
        return self.store.get(project_id)

    def procedural_loop(self, project_id, revision, body):
        kind = v.choice(body.get('kind', 'bob'), '循环类型', ['bob', 'pulse', 'spin', 'shake', 'fade'])
        count = v.integer(body.get('count', 12), '循环帧数', 4, 60)
        amplitude = v.real(body.get('amplitude', 6), '振幅', 0, 100)
        duration = v.duration(body.get('duration', 80))
        with self.store.edit(project_id, revision, '程序化循环') as project:
            frame, layer = find_layer(project, body.get('frame_id'), body.get('layer_id'))
            if layer['locked']:
                raise v.Invalid('图层已锁定')
            if len(project['frames']) + count > 300:
                raise v.Invalid('循环后超过300帧')
            layer_index = frame['layers'].index(layer)
            additions = []
            for i in range(count):
                phase = 2 * math.pi * i / count
                copied = clone_frame(frame)
                copied['name'] = f'{kind} {i + 1}'
                copied['duration'] = duration
                current = copied['layers'][layer_index]
                transform = current['transform']
                if kind == 'bob':
                    transform['y'] += int(round(amplitude * math.sin(phase)))
                elif kind == 'pulse':
                    factor = max(.05, 1 + amplitude / 100 * math.sin(phase))
                    transform['scale_x'] *= factor
                    transform['scale_y'] *= factor
                elif kind == 'spin':
                    transform['rotation'] = i / count * 360
                elif kind == 'shake':
                    transform['x'] += int(round(amplitude * math.sin(phase * 3)))
                elif kind == 'fade':
                    current['opacity'] *= (1 + math.cos(phase)) / 2
                additions.append(copied)
            insertion = project['frames'].index(frame) + 1
            project['frames'][insertion:insertion] = additions
        return self.store.get(project_id)
