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


def create_demo(store, kind='robot'):
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
