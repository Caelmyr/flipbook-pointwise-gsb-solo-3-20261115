import math
import numpy as np
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
from . import validate as v


def change_rgb(image, operation):
    array = np.asarray(image.convert('RGBA')).copy()
    rgb = Image.fromarray(array[:, :, :3])
    converted = operation(rgb)
    array[:, :, :3] = np.asarray(converted.convert('RGB'))
    return Image.fromarray(array)


def replace_color(image, source, target, tolerance=0, preserve_alpha=True):
    array = np.asarray(image.convert('RGBA')).copy()
    distance = np.max(np.abs(array.astype(np.int16) - np.asarray(source, dtype=np.int16)), axis=2)
    selected = distance <= tolerance
    if preserve_alpha:
        array[:, :, :3][selected] = target[:3]
    else:
        array[selected] = target
    return Image.fromarray(array), int(selected.sum())


def alpha_threshold(image, threshold=128):
    array = np.asarray(image.convert('RGBA')).copy()
    array[:, :, 3] = np.where(array[:, :, 3] >= threshold, 255, 0)
    array[array[:, :, 3] == 0] = 0
    return Image.fromarray(array)


def outline(image, radius=1, color=(0, 0, 0, 255), inside=False):
    array = np.asarray(image.convert('RGBA'))
    alpha = Image.fromarray(array[:, :, 3])
    size = radius * 2 + 1
    if inside:
        eroded = np.asarray(alpha.filter(ImageFilter.MinFilter(size)))
        mask = array[:, :, 3].astype(int) - eroded.astype(int)
    else:
        dilated = np.asarray(alpha.filter(ImageFilter.MaxFilter(size)))
        mask = dilated.astype(int) - array[:, :, 3].astype(int)
    ink = Image.new('RGBA', image.size, color)
    ink.putalpha(Image.fromarray(np.clip(mask * color[3] / 255, 0, 255).astype(np.uint8)))
    return Image.alpha_composite(image, ink) if inside else Image.alpha_composite(ink, image)


def shadow(image, dx, dy, radius, color):
    ink = Image.new('RGBA', image.size, color)
    alpha = image.getchannel('A').filter(ImageFilter.GaussianBlur(radius))
    alpha = alpha.point(lambda a: int(a * color[3] / 255))
    ink.putalpha(alpha)
    result = Image.new('RGBA', image.size)
    result.alpha_composite(ink, (dx, dy))
    result.alpha_composite(image)
    return result


def hue_shift(image, degrees):
    rgb = image.convert('RGB').convert('HSV')
    array = np.asarray(rgb).copy()
    hue = array[:, :, 0].astype(int)
    array[:, :, 0] = (hue + int(round(degrees / 360 * 255))) % 256
    result = Image.fromarray(array, mode='HSV').convert('RGB').convert('RGBA')
    result.putalpha(image.getchannel('A'))
    return result


def offset_wrap(image, dx, dy):
    array = np.asarray(image)
    return Image.fromarray(np.roll(array, (dy, dx), axis=(0, 1)))


def pixelate(image, block=2):
    small = image.resize((max(1, image.width // block), max(1, image.height // block)), Image.Resampling.BOX)
    return small.resize(image.size, Image.Resampling.NEAREST)


def posterize(image, levels=4):
    array = np.asarray(image).copy()
    rgb = array[:, :, :3].astype(float)
    rgb = np.rint(rgb / 255 * (levels - 1)) / (levels - 1) * 255
    array[:, :, :3] = np.rint(rgb).astype(np.uint8)
    return Image.fromarray(array)


def alpha_bleed(image, iterations=4):
    array = np.asarray(image).copy()
    known = array[:, :, 3] > 0
    for _ in range(iterations):
        total = np.zeros_like(array[:, :, :3], dtype=float)
        count = np.zeros(known.shape, dtype=float)
        for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            colors = np.roll(array[:, :, :3], (dy, dx), axis=(0, 1))
            valid = np.roll(known, (dy, dx), axis=(0, 1))
            if dy == -1:
                valid[-1] = False
            if dy == 1:
                valid[0] = False
            if dx == -1:
                valid[:, -1] = False
            if dx == 1:
                valid[:, 0] = False
            total += colors * valid[:, :, None]
            count += valid
        grow = ~known & (count > 0)
        array[:, :, :3][grow] = np.rint(total[grow] / count[grow, None]).astype(np.uint8)
        known |= grow
    return Image.fromarray(array)


def apply_effect(image, body):
    operation = v.choice(body.get('operation'), '像素效果', [
        'invert', 'grayscale', 'brightness', 'contrast', 'saturation', 'gamma',
        'hue', 'posterize', 'pixelate', 'blur', 'sharpen', 'outline', 'shadow',
        'alpha_threshold', 'alpha_bleed', 'offset', 'replace', 'flip_x', 'flip_y',
    ])
    amount = v.real(body.get('amount', 1), '强度', 0.01, 10)
    radius = v.integer(body.get('radius', 1), '半径', 1, 12)
    dx = v.integer(body.get('dx', 2), '偏移X', -2048, 2048)
    dy = v.integer(body.get('dy', 2), '偏移Y', -2048, 2048)
    color = v.rgba(body.get('color', '#142136ff'))
    if operation == 'invert':
        return change_rgb(image, ImageOps.invert)
    if operation == 'grayscale':
        return change_rgb(image, lambda im: ImageOps.grayscale(im).convert('RGB'))
    if operation in ('brightness', 'contrast', 'saturation'):
        enhancer = {'brightness': ImageEnhance.Brightness, 'contrast': ImageEnhance.Contrast, 'saturation': ImageEnhance.Color}[operation]
        return change_rgb(image, lambda im: enhancer(im).enhance(amount))
    if operation == 'gamma':
        return change_rgb(image, lambda im: im.point([int(round((i / 255) ** amount * 255)) for i in range(256)] * 3))
    if operation == 'hue':
        return hue_shift(image, v.real(body.get('degrees', 30), '色相角度', -360, 360))
    if operation == 'posterize':
        return posterize(image, v.integer(body.get('levels', 4), '色阶数', 2, 32))
    if operation == 'pixelate':
        return pixelate(image, v.integer(body.get('block', 2), '像素块尺寸', 1, 64))
    if operation == 'blur':
        return image.filter(ImageFilter.GaussianBlur(min(amount, 8)))
    if operation == 'sharpen':
        return image.filter(ImageFilter.UnsharpMask(radius=radius, percent=int(amount * 100), threshold=0))
    if operation == 'outline':
        return outline(image, radius, color, v.boolean(body.get('inside', False), '内描边'))
    if operation == 'shadow':
        return shadow(image, dx, dy, radius, color)
    if operation == 'alpha_threshold':
        return alpha_threshold(image, v.integer(body.get('threshold', 128), '透明度阈值', 0, 255))
    if operation == 'alpha_bleed':
        return alpha_bleed(image, radius)
    if operation == 'offset':
        return offset_wrap(image, dx, dy)
    if operation == 'replace':
        source = v.rgba(body.get('source', '#ffffffff'))
        tolerance = v.integer(body.get('tolerance', 0), '颜色容差', 0, 255)
        return replace_color(image, source, color, tolerance, v.boolean(body.get('preserve_alpha', True), '保留透明度'))[0]
    if operation == 'flip_x':
        return ImageOps.mirror(image)
    return ImageOps.flip(image)
