import colorsys
import numpy as np
from PIL import Image
from . import validate as v
from .model import find_frame
from .composite import render_frame


def color_inventory(images, maximum=256):
    counts = {}
    total = 0
    transparent = 0
    for image in images:
        array = np.asarray(image.convert('RGBA'))
        visible = array[:, :, 3] > 0
        transparent += int(np.count_nonzero(~visible))
        values, frequency = np.unique(array[visible].reshape(-1, 4), axis=0, return_counts=True)
        for color, count in zip(values, frequency):
            key = tuple(int(value) for value in color)
            counts[key] = counts.get(key, 0) + int(count)
            total += int(count)
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    return {
        'unique_colors': len(counts),
        'visible_pixels': total,
        'transparent_pixels': transparent,
        'colors': [
            {'hex': '#%02x%02x%02x%02x' % color, 'count': count, 'percent': round(count / max(total, 1) * 100, 5)}
            for color, count in ordered[:maximum]
        ],
    }


def extract_palette(images, count=16, method='median'):
    samples = []
    for image in images:
        array = np.asarray(image.convert('RGBA'))
        values = array[array[:, :, 3] > 0][:, :3]
        if len(values) > 100000:
            values = values[::int(np.ceil(len(values) / 100000))]
        if len(values):
            samples.append(values)
    if not samples:
        raise v.Invalid('没有可提取颜色的不透明像素')
    pixels = np.concatenate(samples)
    if len(pixels) > 500000:
        pixels = pixels[::int(np.ceil(len(pixels) / 500000))]
    image = Image.fromarray(pixels.reshape(1, -1, 3))
    method_id = Image.Quantize.MEDIANCUT if method == 'median' else Image.Quantize.MAXCOVERAGE
    quantized = image.quantize(colors=count, method=method_id)
    palette = quantized.getpalette()
    usage = sorted(quantized.getcolors(), reverse=True)
    return ['#%02x%02x%02xff' % tuple(palette[index * 3:index * 3 + 3]) for _, index in usage]


def nearest_palette(array, colors):
    values = array.reshape(-1, 3).astype(np.float32)
    palette = np.asarray(colors, dtype=np.float32)
    selected = np.empty(len(values), dtype=np.int32)
    for start in range(0, len(values), 50000):
        block = values[start:start + 50000]
        distance = np.sum((block[:, None, :] - palette[None, :, :]) ** 2, axis=2)
        selected[start:start + len(block)] = np.argmin(distance, axis=1)
    return selected.reshape(array.shape[:2])


def quantize(image, palette, dither='none'):
    colors = np.asarray([v.rgba(color)[:3] for color in palette], dtype=np.uint8)
    array = np.asarray(image.convert('RGBA')).copy()
    if dither == 'none':
        indices = nearest_palette(array[:, :, :3], colors)
        array[:, :, :3] = colors[indices]
        return Image.fromarray(array)
    if dither == 'ordered':
        bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], dtype=float)
        yy, xx = np.indices(array.shape[:2])
        adjusted = np.clip(array[:, :, :3].astype(float) + (bayer[yy % 4, xx % 4] - 7.5)[:, :, None] * 4, 0, 255)
        array[:, :, :3] = colors[nearest_palette(adjusted, colors)]
        return Image.fromarray(array)
    if dither != 'floyd':
        raise v.Invalid('抖动方式不支持')
    if image.width * image.height > 1000000:
        raise v.Invalid('误差扩散抖动限100万像素')
    work = array[:, :, :3].astype(float)
    height, width = work.shape[:2]
    for y in range(height):
        for x in range(width):
            if array[y, x, 3] == 0:
                continue
            original = work[y, x].copy()
            index = int(np.argmin(np.sum((colors.astype(float) - original) ** 2, axis=1)))
            work[y, x] = colors[index]
            error = original - colors[index]
            for dx, dy, weight in [(1, 0, 7 / 16), (-1, 1, 3 / 16), (0, 1, 5 / 16), (1, 1, 1 / 16)]:
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and ny < height and array[ny, nx, 3] > 0:
                    work[ny, nx] = np.clip(work[ny, nx] + error * weight, 0, 255)
    array[:, :, :3] = np.rint(work).astype(np.uint8)
    return Image.fromarray(array)


def ramp(first, second, steps=8, space='rgb'):
    a = np.asarray(v.rgba(first), dtype=float)
    b = np.asarray(v.rgba(second), dtype=float)
    result = []
    if space == 'hsv':
        ah = colorsys.rgb_to_hsv(*(a[:3] / 255))
        bh = colorsys.rgb_to_hsv(*(b[:3] / 255))
    for index in range(steps):
        t = index / (steps - 1)
        alpha = int(round(a[3] * (1 - t) + b[3] * t))
        if space == 'hsv':
            delta = (bh[0] - ah[0] + .5) % 1 - .5
            h = (ah[0] + delta * t) % 1
            s = ah[1] * (1 - t) + bh[1] * t
            value = ah[2] * (1 - t) + bh[2] * t
            rgb = [int(round(v * 255)) for v in colorsys.hsv_to_rgb(h, s, value)]
        else:
            rgb = np.rint(a[:3] * (1 - t) + b[:3] * t).astype(int).tolist()
        result.append('#%02x%02x%02x%02x' % tuple(rgb + [alpha]))
    return result


def cycle_image(image, palette, offset=1):
    colors = [v.rgba(color) for color in palette]
    array = np.asarray(image).copy()
    original = array.copy()
    for index, color in enumerate(colors):
        selected = np.all(original == color, axis=2)
        array[selected] = colors[(index + offset) % len(colors)]
    return Image.fromarray(array)


class Palettes:
    def __init__(self, store):
        self.store = store

    def extract(self, project_id, ids, count=16, method='median'):
        project = self.store.get(project_id)
        ids = v.frame_ids(ids, project)
        count = v.integer(count, '颜色数量', 2, 64)
        v.choice(method, '提取方式', ['median', 'coverage'])
        images = [render_frame(self.store, project, key) for key in ids]
        return {'palette': extract_palette(images, count, method), 'inventory': color_inventory(images)}

    def apply(self, project_id, revision, ids, palette, dither):
        palette = v.palette(palette)
        v.choice(dither, '抖动', ['none', 'ordered', 'floyd'])
        with self.store.edit(project_id, revision, '应用共享调色板') as project:
            ids = v.frame_ids(ids, project)
            from .layer_nodes import walk_layers
            for frame_id in ids:
                frame = find_frame(project, frame_id)
                for layer in walk_layers(frame['layers']):
                    if layer.get('kind') == 'group':
                        continue
                    if layer['locked']:
                        raise v.Invalid('包含锁定图层')
                    from .role_tools import require_ordinary
                    require_ordinary(layer)
                    from .cels import require_detached
                    require_detached(layer)
                    image = self.store.image(project_id, layer['asset'])
                    from .role_tools import require_ordinary
                    require_ordinary(layer)
                    layer['asset'] = self.store.put_image(project_id, quantize(image, palette, dither))
            project['palette'] = palette
        return self.store.get(project_id)

    def cycle(self, project_id, revision, frame_id, count=6):
        count = v.integer(count, '循环帧数', 2, 32)
        from .model import clone_frame
        with self.store.edit(project_id, revision, '调色板循环动画') as project:
            frame = find_frame(project, frame_id)
            if len(project['frames']) + count > 300:
                raise v.Invalid('超过300帧')
            additions = []
            for offset in range(1, count + 1):
                copied = clone_frame(frame)
                copied['name'] = f'颜色循环 {offset}'
                for layer in copied['layers']:
                    from .role_tools import require_ordinary
                    require_ordinary(layer)
                    from .cels import require_detached
                    require_detached(layer)
                    image = self.store.image(project_id, layer['asset'])
                    layer['asset'] = self.store.put_image(project_id, cycle_image(image, project['palette'], offset))
                additions.append(copied)
            index = project['frames'].index(frame) + 1
            project['frames'][index:index] = additions
        return self.store.get(project_id)
