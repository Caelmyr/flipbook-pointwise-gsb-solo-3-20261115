import io
import math
import numpy as np
from PIL import Image, ImageOps, ImageDraw
from . import validate as v
from .model import find_frame


def transform_layer(image, transform, canvas_size):
    if transform['flip_x']:
        image = ImageOps.mirror(image)
    if transform['flip_y']:
        image = ImageOps.flip(image)
    width = max(1, int(round(image.width * transform['scale_x'])))
    height = max(1, int(round(image.height * transform['scale_y'])))
    if width * height > 16777216:
        raise v.Invalid('图层变换后的尺寸过大')
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.NEAREST)
    if transform['rotation']:
        image = image.rotate(-transform['rotation'], resample=Image.Resampling.NEAREST, expand=True)
    result = Image.new('RGBA', canvas_size)
    offset_x = transform['x'] + (canvas_size[0] - image.width) // 2
    offset_y = transform['y'] + (canvas_size[1] - image.height) // 2
    result.paste(image, (offset_x, offset_y))
    return result


def blend_pixels(backdrop, source, mode='normal', opacity=1):
    back = np.asarray(backdrop.convert('RGBA'), dtype=float) / 255
    front = np.asarray(source.convert('RGBA'), dtype=float) / 255
    cb = back[:, :, :3]
    cs = front[:, :, :3]
    ab = back[:, :, 3:4]
    alpha = front[:, :, 3:4] * opacity
    if mode == 'normal':
        blended = cs
    elif mode == 'multiply':
        blended = cb * cs
    elif mode == 'screen':
        blended = cb + cs - cb * cs
    elif mode == 'add':
        blended = np.minimum(cb + cs, 1)
    elif mode == 'difference':
        blended = np.abs(cb - cs)
    elif mode == 'overlay':
        blended = np.where(cb <= .5, 2 * cb * cs, 1 - 2 * (1 - cb) * (1 - cs))
    elif mode == 'darken':
        blended = np.minimum(cb, cs)
    elif mode == 'lighten':
        blended = np.maximum(cb, cs)
    else:
        raise v.Invalid('混合模式不支持')
    result_alpha = alpha + ab * (1 - alpha)
    premultiplied = (1 - alpha) * ab * cb + alpha * ((1 - ab) * cs + ab * blended)
    rgb = np.divide(premultiplied, result_alpha, out=np.zeros_like(premultiplied), where=result_alpha > 1e-10)
    hidden = np.where(np.any(cs != 0, axis=2, keepdims=True), cs, cb)
    rgb = np.where(result_alpha > 1e-10, rgb, hidden)
    result = np.concatenate([rgb, result_alpha], axis=2)
    return Image.fromarray(np.clip(np.rint(result * 255), 0, 255).astype(np.uint8))


def render_frame(store, project, frame_id, background=False):
    frame = find_frame(project, frame_id)
    size = (project['width'], project['height'])
    output = Image.new('RGBA', size, v.rgba(project['background']) if background else (0, 0, 0, 0))
    if any(layer.get('kind') == 'group' or layer.get('affine') for layer in frame['layers']):
        from .layer_tree import render_tree
        return render_tree(frame['layers'], size, lambda asset: store.image(project['id'], asset), output)
    for layer in frame['layers']:
        if not layer['visible'] or layer['opacity'] <= 0:
            continue
        from .role_tools import display_image
        if layer.get('scene_source'):
            from .composition import render_external
            image = render_external(store, layer['scene_source'])
        else:
            image = display_image(store, project, layer)
        transformed = transform_layer(image, layer['transform'], size)
        output = blend_pixels(output, transformed, layer['blend'], layer['opacity'])
    return output


def onion_skin(store, project, frame_id, previous=1, following=1, opacity=.3):
    index = next(i for i, frame in enumerate(project['frames']) if frame['id'] == frame_id)
    output = Image.new('RGBA', (project['width'], project['height']))
    for delta in range(-previous, following + 1):
        if delta == 0 or not 0 <= index + delta < len(project['frames']):
            continue
        image = render_frame(store, project, project['frames'][index + delta]['id'])
        array = np.asarray(image).copy()
        tint = (245, 102, 145) if delta < 0 else (86, 158, 255)
        array[:, :, :3] = tint
        array[:, :, 3] = np.rint(array[:, :, 3] * opacity / abs(delta)).astype(np.uint8)
        output.alpha_composite(Image.fromarray(array))
    output.alpha_composite(render_frame(store, project, frame_id))
    return output


def checkerboard(size, tile=8):
    image = Image.new('RGBA', size, '#23323fff')
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], tile):
        for x in range(0, size[0], tile):
            if (x // tile + y // tile) % 2:
                draw.rectangle((x, y, x + tile - 1, y + tile - 1), fill='#304452ff')
    return image


def preview(image, zoom=1, checker=True, grid=False, grid_size=8):
    if zoom < 1 or zoom > 16:
        raise v.Invalid('预览放大倍率为1至16')
    if image.width * image.height * zoom * zoom > 16000000:
        raise v.Invalid('预览超过1600万像素')
    if checker:
        background = checkerboard(image.size)
        background.alpha_composite(image)
        image = background
    size = (image.width * zoom, image.height * zoom)
    image = image.resize(size, Image.Resampling.NEAREST)
    if grid and zoom >= 3:
        draw = ImageDraw.Draw(image)
        spacing = grid_size * zoom
        for x in range(0, image.width, spacing):
            draw.line((x, 0, x, image.height), fill=(150, 195, 215, 90))
        for y in range(0, image.height, spacing):
            draw.line((0, y, image.width, y), fill=(150, 195, 215, 90))
    return image


def difference(first, second, gain=3):
    if first.size != second.size:
        raise v.Invalid('比较图像尺寸必须相同')
    a = np.asarray(first, dtype=float)
    b = np.asarray(second, dtype=float)
    diff = np.abs(a - b)
    rgb = np.clip(diff[:, :, :3] * gain, 0, 255).astype(np.uint8)
    alpha = np.maximum(diff[:, :, 3], diff[:, :, :3].max(axis=2))
    output = np.concatenate([rgb, np.maximum(alpha, 80)[:, :, None].astype(np.uint8)], axis=2)
    return Image.fromarray(output), {
        'changed_pixels': int(np.count_nonzero(np.any(diff > 0, axis=2))),
        'changed_percent': round(float(np.mean(np.any(diff > 0, axis=2))) * 100, 4),
        'mean_absolute_rgba_difference': round(float(diff.mean()), 6),
    }


def png_bytes(image):
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def trim_bounds(image, threshold=0):
    alpha = np.asarray(image.convert('RGBA'))[:, :, 3]
    y, x = np.nonzero(alpha > threshold)
    if not len(x):
        return None
    return [int(x.min()), int(y.min()), int(x.max()) + 1, int(y.max()) + 1]


def opaque_bounds_union(images, threshold=0):
    result = None
    for image in images:
        bounds = trim_bounds(image, threshold)
        if bounds is None:
            continue
        if result is None:
            result = bounds
        else:
            result = [min(result[0], bounds[0]), min(result[1], bounds[1]), max(result[2], bounds[2]), max(result[3], bounds[3])]
    return result
