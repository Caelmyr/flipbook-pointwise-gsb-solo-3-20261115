from collections import deque
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from . import validate as v


def font(size):
    paths = [
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        'C:/Windows/Fonts/arial.ttf',
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def stroke_mask(size, points, width):
    mask = Image.new('L', size, 0)
    draw = ImageDraw.Draw(mask)
    points = [tuple(p) for p in points]
    if len(points) > 1:
        draw.line(points, fill=255, width=width, joint='curve')
    radius = (width - 1) / 2
    for x, y in points:
        if width == 1:
            draw.point((int(round(x)), int(round(y))), fill=255)
        else:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return mask


def apply_paint(image, mask, color):
    ink = Image.new('RGBA', image.size, color)
    alpha = np.asarray(mask, dtype=float) / 255 * color[3]
    ink.putalpha(Image.fromarray(np.rint(alpha).astype(np.uint8)))
    return Image.alpha_composite(image, ink)


def erase_pixels(image, mask):
    array = np.asarray(image).copy()
    factor = 1 - np.asarray(mask, dtype=float) / 255
    array[:, :, 3] = np.rint(array[:, :, 3] * factor).astype(np.uint8)
    array[array[:, :, 3] == 0] = 0
    return Image.fromarray(array)


def flood_mask(image, position, tolerance=0, contiguous=True):
    array = np.asarray(image.convert('RGBA'), dtype=np.int16)
    x, y = [int(round(value)) for value in position]
    target = array[y, x]
    distance = np.max(np.abs(array - target), axis=2)
    matching = distance <= tolerance
    if not contiguous:
        return Image.fromarray(matching.astype(np.uint8) * 255)
    height, width = matching.shape
    visited = np.zeros(matching.shape, dtype=bool)
    queue = deque([(x, y)])
    visited[y, x] = True
    while queue:
        px, py = queue.popleft()
        for nx, ny in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
            if not 0 <= nx < width or not 0 <= ny < height:
                continue
            if visited[ny, nx] or not matching[ny, nx]:
                continue
            visited[ny, nx] = True
            queue.append((nx, ny))
    return Image.fromarray(visited.astype(np.uint8) * 255)


def gradient_image(size, start, end, first, second):
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator < 1e-9:
        raise v.Invalid('渐变起点和终点不能重合')
    yy, xx = np.indices((size[1], size[0]))
    factor = np.clip(((xx - start[0]) * dx + (yy - start[1]) * dy) / denominator, 0, 1)
    a = np.asarray(first)
    b = np.asarray(second)
    result = a[None, None, :] * (1 - factor[:, :, None]) + b[None, None, :] * factor[:, :, None]
    return Image.fromarray(np.rint(result).astype(np.uint8))


def mirrored_paths(points, width, height, mirror_x, mirror_y):
    paths = [points]
    if mirror_x:
        paths.append([[width - 1 - x, y] for x, y in points])
    if mirror_y:
        paths += [[[x, height - 1 - y] for x, y in path] for path in paths[:]]
    unique = []
    seen = set()
    for path in paths:
        key = tuple((round(x, 4), round(y, 4)) for x, y in path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def shape_mask(size, command, points):
    """Build the coverage mask (L image) that the command paints with."""
    mode = command['tool']
    mask = Image.new('L', size, 0)
    draw = ImageDraw.Draw(mask)
    width = command['size']
    if mode in ('pencil', 'erase', 'line'):
        mask = stroke_mask(size, points if mode != 'line' else [points[0], points[-1]], width)
    elif mode in ('rectangle', 'ellipse'):
        first, last = points[0], points[-1]
        box = (min(first[0], last[0]), min(first[1], last[1]), max(first[0], last[0]), max(first[1], last[1]))
        kwargs = {'fill': 255} if command['filled'] else {'outline': 255, 'width': width}
        if mode == 'rectangle':
            draw.rectangle(box, **kwargs)
        else:
            draw.ellipse(box, **kwargs)
    elif mode == 'polygon':
        draw.polygon([tuple(p) for p in points], fill=255 if command['filled'] else None, outline=255, width=width)
    elif mode == 'text':
        draw.text(tuple(points[0]), command['text'], fill=255, font=font(command['font_size']))
    elif mode == 'fill':
        mask = flood_mask(Image.new('RGBA', size), points[0], command['tolerance'], command['contiguous'])
    return mask


def draw_shape(image, command, points):
    mode = command['tool']
    if mode == 'gradient':
        return Image.alpha_composite(image, gradient_image(image.size, points[0], points[-1], command['color'], command['secondary']))
    if mode == 'fill':
        mask = flood_mask(image, points[0], command['tolerance'], command['contiguous'])
    else:
        mask = shape_mask(image.size, command, points)
    if mode == 'erase':
        return erase_pixels(image, mask)
    return apply_paint(image, mask, command['color'])


def draw(image, command):
    paths = mirrored_paths(command['points'], image.width, image.height, command['mirror_x'], command['mirror_y'])
    result = image.convert('RGBA')
    for path in paths:
        result = draw_shape(result, command, path)
    return result


def selection_mask(size, body):
    mask = Image.new('L', size, 0)
    painter = ImageDraw.Draw(mask)
    kind = body.get('type', 'rectangle')
    if kind == 'rectangle':
        rectangle = v.rect(body, *size)
        x, y = rectangle['x'], rectangle['y']
        painter.rectangle((x, y, x + rectangle['width'] - 1, y + rectangle['height'] - 1), fill=255)
    elif kind == 'polygon':
        points = v.points(body.get('points'), *size, minimum=3)
        painter.polygon([tuple(p) for p in points], fill=255)
    elif kind == 'ellipse':
        rectangle = v.rect(body, *size)
        x, y = rectangle['x'], rectangle['y']
        painter.ellipse((x, y, x + rectangle['width'] - 1, y + rectangle['height'] - 1), fill=255)
    else:
        raise v.Invalid('选区类型不支持')
    return mask


def move_selection(image, selection, dx, dy, copy=False):
    mask = selection_mask(image.size, selection)
    selected = image.copy()
    alpha = np.asarray(image.getchannel('A'), dtype=float) * np.asarray(mask, dtype=float) / 255
    selected.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    base = image.copy() if copy else erase_pixels(image, mask)
    shifted = Image.new('RGBA', image.size)
    shifted.alpha_composite(selected, (dx, dy))
    return Image.alpha_composite(base, shifted)


def stamp(image, source, x, y, scale=1, rotation=0, opacity=1):
    width = max(1, int(round(source.width * scale)))
    height = max(1, int(round(source.height * scale)))
    if width * height > 4194304:
        raise v.Invalid('印章尺寸过大')
    source = source.resize((width, height), Image.Resampling.NEAREST)
    if rotation:
        source = source.rotate(-rotation, resample=Image.Resampling.NEAREST, expand=True)
    if opacity < 1:
        source.putalpha(source.getchannel('A').point(lambda a: int(round(a * opacity))))
    result = image.copy()
    result.alpha_composite(source, (x, y))
    return result
