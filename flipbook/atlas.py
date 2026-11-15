import math
from dataclasses import dataclass
from PIL import Image
from . import validate as v
from .composite import render_frame, trim_bounds


@dataclass
class Rectangle:
    x: int
    y: int
    width: int
    height: int

    def contains(self, other):
        return self.x <= other.x and self.y <= other.y and self.x + self.width >= other.x + other.width and self.y + self.height >= other.y + other.height

    def intersects(self, other):
        return self.x < other.x + other.width and self.x + self.width > other.x and self.y < other.y + other.height and self.y + self.height > other.y


class MaxRects:
    def __init__(self, width, height, rotate=False):
        self.width = width
        self.height = height
        self.rotate = rotate
        self.free = [Rectangle(0, 0, width, height)]
        self.used = []

    def insert(self, width, height):
        candidate = None
        best = None
        rotated = False
        for free in self.free:
            orientations = [(width, height, False)]
            if self.rotate and width != height:
                orientations.append((height, width, True))
            for w, h, is_rotated in orientations:
                if w > free.width or h > free.height:
                    continue
                score = (min(free.width - w, free.height - h), max(free.width - w, free.height - h), free.y, free.x)
                if best is None or score < best:
                    best = score
                    candidate = Rectangle(free.x, free.y, w, h)
                    rotated = is_rotated
        if candidate is None:
            return None
        self.split(candidate)
        self.prune()
        self.used.append(candidate)
        return candidate, rotated

    def reserve(self, rectangle):
        if rectangle.x < 0 or rectangle.y < 0 or rectangle.x + rectangle.width > self.width or rectangle.y + rectangle.height > self.height:
            return False
        if any(rectangle.intersects(used) for used in self.used):
            return False
        self.split(rectangle)
        self.prune()
        self.used.append(Rectangle(rectangle.x, rectangle.y, rectangle.width, rectangle.height))
        return True

    def split(self, used):
        next_free = []
        for free in self.free:
            if not free.intersects(used):
                next_free.append(free)
                continue
            if used.x > free.x:
                next_free.append(Rectangle(free.x, free.y, used.x - free.x, free.height))
            if used.x + used.width < free.x + free.width:
                next_free.append(Rectangle(used.x + used.width, free.y, free.x + free.width - used.x - used.width, free.height))
            if used.y > free.y:
                next_free.append(Rectangle(free.x, free.y, free.width, used.y - free.y))
            if used.y + used.height < free.y + free.height:
                next_free.append(Rectangle(free.x, used.y + used.height, free.width, free.y + free.height - used.y - used.height))
        self.free = [r for r in next_free if r.width > 0 and r.height > 0]

    def prune(self):
        remaining = []
        for index, rectangle in enumerate(self.free):
            if any(index != other_index and other.contains(rectangle) and (other != rectangle or other_index < index) for other_index, other in enumerate(self.free)):
                continue
            remaining.append(rectangle)
        self.free = remaining


def next_power_two(value):
    return 1 << max(0, (value - 1).bit_length())


def extrusion(canvas, image, x, y, size):
    canvas.paste(image, (x, y))
    if size <= 0:
        return
    width, height = image.size
    canvas.paste(image.crop((0, 0, width, 1)).resize((width, size)), (x, y - size))
    canvas.paste(image.crop((0, height - 1, width, height)).resize((width, size)), (x, y + height))
    canvas.paste(image.crop((0, 0, 1, height)).resize((size, height)), (x - size, y))
    canvas.paste(image.crop((width - 1, 0, width, height)).resize((size, height)), (x + width, y))
    corners = [(0, 0, x - size, y - size), (width - 1, 0, x + width, y - size), (0, height - 1, x - size, y + height), (width - 1, height - 1, x + width, y + height)]
    for sx, sy, dx, dy in corners:
        canvas.paste(Image.new('RGBA', (size, size), image.getpixel((sx, sy))), (dx, dy))


def prepare_sprites(store, project, ids, trim=True):
    sprites = []
    for index, frame_id in enumerate(ids):
        image = render_frame(store, project, frame_id)
        bounds = trim_bounds(image) if trim else [0, 0, image.width, image.height]
        empty = bounds is None
        if bounds is None:
            bounds = [0, 0, 1, 1]
        frame = next(frame for frame in project['frames'] if frame['id'] == frame_id)
        sprites.append({
            'id': frame_id,
            'index': index,
            'name': frame['name'],
            'image': image.crop(bounds),
            'bounds': bounds,
            'source_size': image.size,
            'duration': frame['duration'],
            'pivot': frame['pivot'],
            'boxes': frame['boxes'],
            'empty': empty,
        })
    return sprites


def pack_attempt(sprites, size, padding, extrude, rotate):
    packer = MaxRects(size[0], size[1], rotate)
    placement = []
    border = padding + extrude
    order = sorted(sprites, key=lambda s: (max(s['image'].size), s['image'].width * s['image'].height), reverse=True)
    for sprite in order:
        inserted = packer.insert(sprite['image'].width + border * 2, sprite['image'].height + border * 2)
        if inserted is None:
            return None
        rectangle, rotated = inserted
        placement.append((sprite, rectangle, rotated))
    return placement


def automatic_size(sprites, padding, extrude, rotate, maximum=4096):
    border = padding + extrude
    minimum_w = max(s['image'].width + 2 * border for s in sprites)
    minimum_h = max(s['image'].height + 2 * border for s in sprites)
    area = sum((s['image'].width + 2 * border) * (s['image'].height + 2 * border) for s in sprites)
    candidates = []
    width = next_power_two(minimum_w)
    while width <= maximum:
        height = next_power_two(minimum_h)
        while height <= maximum:
            if width * height >= area:
                candidates.append((width, height))
            height *= 2
        width *= 2
    candidates.sort(key=lambda size: (size[0] * size[1], abs(size[0] - size[1])))
    for size in candidates:
        placement = pack_attempt(sprites, size, padding, extrude, rotate)
        if placement is not None:
            return size, placement
    raise v.Invalid('图集无法在4096×4096内放下，请减少帧或画布尺寸')


def build_atlas(store, project, body):
    ids = v.frame_ids(body.get('frame_ids', [f['id'] for f in project['frames']]), project)
    trim = v.boolean(body.get('trim', True), '裁去透明边缘')
    rotate = v.boolean(body.get('rotate', False), '允许旋转')
    padding = v.integer(body.get('padding', 1), '间隔', 0, 16)
    extrude = v.integer(body.get('extrude', 1), '边缘外扩', 0, 8)
    sprites = prepare_sprites(store, project, ids, trim)
    manual = body.get('size')
    if manual:
        values = v.array(manual, '图集尺寸', 2)
        if len(values) != 2:
            raise v.Invalid('尺寸需要宽和高')
        size = tuple(v.integer(a, '图集边长', 8, 4096) for a in values)
        placement = pack_attempt(sprites, size, padding, extrude, rotate)
        if placement is None:
            raise v.Invalid('指定图集放不下所有精灵')
    else:
        size, placement = automatic_size(sprites, padding, extrude, rotate)
    image = Image.new('RGBA', size)
    entries = []
    border = padding + extrude
    occupied = 0
    for sprite, rectangle, rotated in placement:
        raster = sprite['image']
        if rotated:
            raster = raster.transpose(Image.Transpose.ROTATE_270)
        x, y = rectangle.x + border, rectangle.y + border
        extrusion(image, raster, x, y, extrude)
        occupied += raster.width * raster.height
        bounds = sprite['bounds']
        entries.append({
            'id': sprite['id'],
            'index': sprite['index'],
            'name': sprite['name'],
            'frame': {'x': x, 'y': y, 'width': raster.width, 'height': raster.height},
            'rotated_clockwise': rotated,
            'trimmed': trim,
            'empty': sprite['empty'],
            'source_size': {'width': sprite['source_size'][0], 'height': sprite['source_size'][1]},
            'sprite_source': {'x': bounds[0], 'y': bounds[1], 'width': bounds[2] - bounds[0], 'height': bounds[3] - bounds[1]},
            'duration': sprite['duration'],
            'pivot': sprite['pivot'],
            'boxes': sprite['boxes'],
        })
    metadata = {
        'format': 'flipbook-atlas-v1',
        'size': {'width': size[0], 'height': size[1]},
        'image': 'atlas.png',
        'frames': sorted(entries, key=lambda e: e['index']),
        'padding': padding,
        'extrude': extrude,
        'occupancy_percent': round(occupied / (size[0] * size[1]) * 100, 4),
        'clips': project['clips'],
    }
    return image, metadata


def grid_sheet(store, project, ids, columns=8, spacing=0, margin=0, scale=1):
    ids = v.frame_ids(ids, project)
    columns = min(v.integer(columns, '列数', 1, 64), len(ids))
    spacing = v.integer(spacing, '间隔', 0, 32)
    margin = v.integer(margin, '边距', 0, 64)
    scale = v.integer(scale, '整数放大', 1, 8)
    rows = math.ceil(len(ids) / columns)
    width = (project['width'] + spacing) * columns - spacing + 2 * margin
    height = (project['height'] + spacing) * rows - spacing + 2 * margin
    if width * height * scale ** 2 > 16777216:
        raise v.Invalid('序列图输出超过1600万像素')
    image = Image.new('RGBA', (width, height))
    entries = []
    for index, frame_id in enumerate(ids):
        x = margin + index % columns * (project['width'] + spacing)
        y = margin + index // columns * (project['height'] + spacing)
        frame_image = render_frame(store, project, frame_id)
        image.paste(frame_image, (x, y))
        entries.append({'id': frame_id, 'x': x * scale, 'y': y * scale, 'width': project['width'] * scale, 'height': project['height'] * scale})
    if scale > 1:
        image = image.resize((width * scale, height * scale), Image.Resampling.NEAREST)
    return image, {'frames': entries, 'columns': columns, 'rows': rows, 'scale': scale, 'image': 'sheet.png'}
