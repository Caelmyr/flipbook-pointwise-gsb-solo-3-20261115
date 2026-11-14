import math
from PIL import Image, ImageDraw
from . import validate as v
from .model import new_layer, new_frame, new_id


PALETTE = ['#152437ff', '#66e2bdff', '#f8cc65ff', '#8b9cffff', '#f58cabff', '#ffffffff']


def robot(size, phase):
    image = Image.new('RGBA', size)
    draw = ImageDraw.Draw(image)
    sx, sy = size[0] / 64, size[1] / 64
    def box(bounds, fill):
        scaled = [int(round(value * (sx if i % 2 == 0 else sy))) for i, value in enumerate(bounds)]
        draw.rectangle((min(scaled[0], scaled[2]), min(scaled[1], scaled[3]), max(scaled[0], scaled[2]), max(scaled[1], scaled[3])), fill=fill)
    bob = round(math.sin(phase) * 3)
    box((18, 23 + bob, 45, 44 + bob), PALETTE[1])
    box((22, 17 + bob, 41, 25 + bob), PALETTE[1])
    box((20, 25 + bob, 43, 35 + bob), PALETTE[0])
    blink = abs(math.sin(phase / 2)) < .15
    eye_h = 1 if blink else 5
    box((24, 28 + bob, 27, 28 + bob + eye_h), PALETTE[2])
    box((36, 28 + bob, 39, 28 + bob + eye_h), PALETTE[2])
    box((27, 39 + bob, 36, 41 + bob), PALETTE[0])
    arm = round(math.sin(phase) * 4)
    box((12, 28 + bob + arm, 17, 40 + bob + arm), PALETTE[3])
    box((46, 28 + bob - arm, 51, 40 + bob - arm), PALETTE[3])
    step = round(math.sin(phase) * 3)
    box((23, 45 + bob, 28, 50 + step), PALETTE[3])
    box((35, 45 + bob, 40, 50 - step), PALETTE[3])
    box((20, 51 + step, 29, 54 + step), PALETTE[0])
    box((34, 51 - step, 43, 54 - step), PALETTE[0])
    box((30, 12 + bob, 33, 17 + bob), PALETTE[3])
    box((29, 10 + bob, 34, 13 + bob), PALETTE[2])
    return image


def flame(size, phase):
    image = Image.new('RGBA', size)
    draw = ImageDraw.Draw(image)
    cx, cy = size[0] / 2, size[1] * .62
    for layer, color in [(1, '#f66e67ff'), (.72, '#f9aa50ff'), (.42, '#ffdf7aff')]:
        points = []
        count = 24
        for index in range(count):
            angle = index / count * math.pi * 2
            radius_x = size[0] * .22 * layer
            radius_y = size[1] * .31 * layer
            wobble = 1 + .13 * math.sin(angle * 5 + phase * 2)
            x = cx + math.cos(angle) * radius_x * wobble
            y = cy + math.sin(angle) * radius_y * wobble
            if math.sin(angle) < 0:
                x += math.sin(phase) * size[0] * .08 * -math.sin(angle)
            points.append((int(x), int(y)))
        draw.polygon(points, fill=color)
    return image


def orb(size, phase):
    image = Image.new('RGBA', size)
    draw = ImageDraw.Draw(image)
    cx = size[0] / 2 + math.cos(phase) * size[0] * .12
    cy = size[1] / 2 + math.sin(phase) * size[1] * .12
    radius = min(size) * .18
    for index, color in [(3, '#758aff44'), (2, '#8a9bff88'), (1, '#b9c7ffff')]:
        r = radius * (1 + (index - 1) * .3)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    draw.rectangle((cx - radius / 3, cy - radius / 3, cx, cy), fill='#ffffffff')
    return image


def tiles(size, index):
    image = Image.new('RGBA', size, '#29434bff')
    draw = ImageDraw.Draw(image)
    cell = max(2, size[0] // 8)
    if index % 3 == 0:
        draw.rectangle((0, 0, size[0] - 1, cell * 2 - 1), fill='#66c995ff')
        for x in range(0, size[0], cell * 2):
            draw.rectangle((x, cell * 2, x + cell - 1, cell * 3), fill='#438966ff')
    elif index % 3 == 1:
        for y in range(0, size[1], cell * 2):
            shift = cell if y // cell % 4 else 0
            for x in range(-cell, size[0], cell * 3):
                draw.rectangle((x + shift, y, x + shift + cell * 3 - 2, y + cell * 2 - 2), fill='#73889aff')
    else:
        for y in range(0, size[1], cell):
            for x in range(0, size[0], cell):
                color = '#548bcaff' if (x + y) // cell % 2 else '#68a5d8ff'
                draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=color)
    return image


def depth_scene(size):
    """Four separate planes for the multiplane camera demo: sky, hills, character, foreground."""
    width, height = size
    sky = Image.new('RGBA', size)
    draw = ImageDraw.Draw(sky)
    for y in range(height):
        blend = y / max(1, height - 1)
        color = (int(22 + 26 * blend), int(33 + 48 * blend), int(58 + 64 * blend), 255)
        draw.line((0, y, width, y), fill=color)
    for x, y in [(9, 12), (21, 7), (34, 15), (47, 9), (58, 18), (70, 6), (83, 13), (92, 20)]:
        draw.point((x, y), fill='#e8f3ffff')
    hills = Image.new('RGBA', size)
    draw = ImageDraw.Draw(hills)
    for base, color, step in [(height * .56, '#2c5566ff', 9), (height * .68, '#203f50ff', 7)]:
        points = [(0, height)]
        for x in range(0, width + 1, step):
            y = base + math.sin(x / 11) * 5 + math.cos(x / 7) * 2
            points.append((x, int(y)))
        points.append((width, height))
        draw.polygon(points, fill=color)
    for x in range(4, width, 13):
        draw.rectangle((x, int(height * .72), x + 1, int(height * .78)), fill='#8aa7b5ff')
    character = Image.new('RGBA', size)
    draw = ImageDraw.Draw(character)
    cx, cy = width // 2, int(height * .62)
    draw.ellipse((cx - 9, cy + 12, cx + 9, cy + 17), fill='#00000066')
    draw.rectangle((cx - 4, cy - 10, cx + 4, cy + 4), fill='#66e2bdff')
    draw.rectangle((cx - 6, cy - 18, cx + 6, cy - 10), fill='#66e2bdff')
    draw.rectangle((cx - 5, cy - 16, cx + 5, cy - 12), fill='#152437ff')
    draw.rectangle((cx - 3, cy - 15, cx - 1, cy - 13), fill='#f8cc65ff')
    draw.rectangle((cx + 1, cy - 15, cx + 3, cy - 13), fill='#f8cc65ff')
    draw.rectangle((cx - 4, cy + 4, cx - 2, cy + 12), fill='#8b9cffff')
    draw.rectangle((cx + 2, cy + 4, cx + 4, cy + 12), fill='#8b9cffff')
    draw.rectangle((cx - 8, cy - 6, cx - 5, cy + 2), fill='#8b9cffff')
    draw.rectangle((cx + 5, cy - 6, cx + 8, cy + 2), fill='#8b9cffff')
    draw.rectangle((cx - 1, cy - 22, cx + 1, cy - 18), fill='#f8cc65ff')
    foreground = Image.new('RGBA', size)
    draw = ImageDraw.Draw(foreground)
    draw.rectangle((0, height - 8, width, height), fill='#173a2dff')
    for x in range(0, width, 5):
        draw.line((x, height - 8, x + 2, height - 14), fill='#2b6b4bff')
    draw.polygon([(0, height), (0, height - 26), (16, height - 20), (26, height)], fill='#0f2d23ff')
    draw.polygon([(width - 30, height), (width - 22, height - 30), (width, height - 24), (width, height)], fill='#0f2d23ff')
    return {'天空': sky, '远山': hills, '角色': character, '前景': foreground}


def create_depth_demo(store):
    size = (96, 64)
    project = store.create('纵深街道 · 多平面摄影机', *size)
    with store.edit(project['id'], 0, '创建多平面纵深演示') as working:
        working['frames'] = []
        working['palette'] = PALETTE[:]
        blank = store.put_image(project['id'], Image.new('RGBA', size))
        frame = new_frame(blank, '镜头原画', 1200)
        frame['layers'] = []
        depths = {'天空': 320.0, '远山': 180.0, '角色': 90.0, '前景': 50.0}
        for name, image in depth_scene(size).items():
            asset = store.put_image(project['id'], image)
            frame['layers'].append(new_layer(name, asset))
        working['frames'].append(frame)
        working['clips'] = [{'id': new_id(), 'name': '纵深推进', 'frame_ids': [frame['id']],
                             'direction': 'forward', 'repeat': 0}]
        working['camera'] = {
            'enabled': True,
            'source_frame': frame['id'],
            'duration': 1600,
            'fps': 12,
            'near': 2.0,
            'sampling': 'nearest',
            'panes': [{'layer_id': layer['id'], 'depth': depths[layer['name']], 'visible': True,
                       'cover': layer['name'] in ('天空', '远山')}
                      for layer in frame['layers']],
            'keys': [
                {'time': 0, 'x': -10, 'y': 2, 'z': 0, 'focal': 100, 'easing': 'ease_in_out'},
                {'time': 800, 'x': 0, 'y': 0, 'z': 10, 'focal': 104, 'easing': 'ease_in_out'},
                {'time': 1600, 'x': 12, 'y': -2, 'z': 20, 'focal': 106, 'easing': 'ease_out'},
            ],
        }
        working['notes'] = '背景、远山、角色和前景分为独立图层，用多平面摄影机同时推进与横移。图层像素与位置保留，重新调镜头从原画计算。'
    return store.get(project['id'])


def create_demo(store, kind='robot'):
    if kind == 'depth':
        return create_depth_demo(store)
    v.choice(kind, '演示主题', ['robot', 'flame', 'orb', 'tiles'])
    names = {'robot': '薄荷机器人 · 像素行走', 'flame': '火焰 · 循环特效', 'orb': '星能球 · 透明动画', 'tiles': '地形瓦片 · 关卡素材'}
    size = (64, 64) if kind != 'tiles' else (32, 32)
    project = store.create(names[kind], *size)
    count = 12 if kind != 'tiles' else 3
    with store.edit(project['id'], 0, '创建原创合成演示') as working:
        working['frames'] = []
        working['palette'] = PALETTE[:]
        for index in range(count):
            phase = index / count * math.pi * 2
            image = robot(size, phase) if kind == 'robot' else flame(size, phase) if kind == 'flame' else orb(size, phase) if kind == 'orb' else tiles(size, index)
            asset = store.put_image(project['id'], image)
            frame = new_frame(asset, f'{kind} {index + 1:02d}', 90 if kind != 'tiles' else 200)
            if kind == 'robot':
                frame['boxes'] = [{'name': '主体', 'x': 18, 'y': 18, 'width': 28, 'height': 37}]
                frame['pivot'] = [.5, .85]
            working['frames'].append(frame)
        working['clips'] = [{
            'id': new_id(),
            'name': '默认循环',
            'frame_ids': [frame['id'] for frame in working['frames']],
            'direction': 'forward',
            'repeat': 0,
        }]
        working['notes'] = '程序绘制的原创演示素材，可在本项目内编辑、导出及复用。'
    return store.get(project['id'])
