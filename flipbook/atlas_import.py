import io
import json
from PIL import Image
from . import validate as v
from .model import new_frame
from .importing import decode_sequence, fit_canvas


def validate_atlas(metadata, image_size):
    metadata = v.object_value(metadata, '图集元数据')
    if metadata.get('format') != 'flipbook-atlas-v1':
        raise v.Invalid('当前仅支持本项目flipbook-atlas-v1图集')
    declared = v.object_value(metadata.get('size'), '图集尺寸')
    width = v.integer(declared.get('width'), '图集宽度', 1, 4096)
    height = v.integer(declared.get('height'), '图集高度', 1, 4096)
    if (width, height) != image_size:
        raise v.Invalid('图集图像与元数据尺寸不一致')
    entries = v.array(metadata.get('frames'), '精灵条目', 300)
    if not entries:
        raise v.Invalid('图集没有精灵条目')
    output = []
    seen = set()
    for index, entry in enumerate(entries):
        entry = v.object_value(entry, '精灵条目')
        identifier = v.key(entry.get('id'))
        if identifier in seen:
            raise v.Invalid('精灵编号重复')
        seen.add(identifier)
        rectangle = v.rect(entry.get('frame'), width, height)
        source = v.object_value(entry.get('source_size'), '原始画布尺寸')
        source_w = v.integer(source.get('width'), '原始宽度', 8, 2048)
        source_h = v.integer(source.get('height'), '原始高度', 8, 2048)
        trimmed = v.rect(entry.get('sprite_source'), source_w, source_h)
        rotated = v.boolean(entry.get('rotated_clockwise', False), '精灵旋转标记')
        expected = (trimmed['height'], trimmed['width']) if rotated else (trimmed['width'], trimmed['height'])
        if (rectangle['width'], rectangle['height']) != expected:
            raise v.Invalid('精灵矩形尺寸与裁边或旋转记录不一致')
        pivot = v.array(entry.get('pivot', [.5, .5]), '枢轴', 2)
        if len(pivot) != 2:
            raise v.Invalid('枢轴需要两个分量')
        pivot = [v.real(value, '枢轴', 0, 1) for value in pivot]
        boxes = []
        for box in v.array(entry.get('boxes', []), '碰撞框', 30):
            checked = v.rect(box, source_w, source_h)
            checked['name'] = v.text(box.get('name', '碰撞框'), '碰撞框名称', 60)
            boxes.append(checked)
        output.append({
            'old_id': identifier,
            'index': v.integer(entry.get('index', index), '条目顺序', 0, 10000),
            'name': v.text(entry.get('name', f'精灵{index + 1}'), '精灵名称', 100, False),
            'rectangle': rectangle,
            'source_size': (source_w, source_h),
            'trimmed': trimmed,
            'rotated': rotated,
            'pivot': pivot,
            'boxes': boxes,
            'duration': v.duration(entry.get('duration', 100)),
        })
    return sorted(output, key=lambda entry: entry['index'])


def unpack_sprite(image, entry):
    rectangle = entry['rectangle']
    x, y, width, height = [rectangle[key] for key in ['x', 'y', 'width', 'height']]
    sprite = image.crop((x, y, x + width, y + height))
    if entry['rotated']:
        sprite = sprite.transpose(Image.Transpose.ROTATE_90)
    output = Image.new('RGBA', entry['source_size'])
    output.paste(sprite, (entry['trimmed']['x'], entry['trimmed']['y']))
    return output


def import_atlas(store, stream, metadata):
    try:
        with Image.open(stream) as decoded:
            if decoded.width > 4096 or decoded.height > 4096:
                raise v.Invalid('图集最大4096×4096')
            image = decoded.convert('RGBA')
            image.load()
    except OSError as error:
        raise v.Invalid('图集图像无法解码') from error
    entries = validate_atlas(metadata, image.size)
    sizes = {entry['source_size'] for entry in entries}
    if len(sizes) != 1:
        raise v.Invalid('导入动画要求图集各精灵的原始画布尺寸相同')
    width, height = next(iter(sizes))
    v.pixel_budget(width, height, len(entries))
    created = store.create('从精灵图集恢复', width, height)
    id_mapping = {}
    with store.edit(created['id'], 0, '恢复精灵图集') as project:
        project['frames'] = []
        for entry in entries:
            sprite = unpack_sprite(image, entry)
            frame = new_frame(store.put_image(created['id'], sprite), entry['name'], entry['duration'])
            frame['pivot'] = entry['pivot']
            frame['boxes'] = entry['boxes']
            id_mapping[entry['old_id']] = frame['id']
            project['frames'].append(frame)
        from .model import new_id
        clips = []
        for source_clip in v.array(metadata.get('clips', []), '图集片段', 100):
            ids = [id_mapping[key] for key in source_clip.get('frame_ids', []) if key in id_mapping]
            if not ids:
                continue
            clips.append({
                'id': new_id(),
                'name': v.text(source_clip.get('name', '片段'), '片段名称', 100, False),
                'frame_ids': list(dict.fromkeys(ids)),
                'direction': v.choice(source_clip.get('direction', 'forward'), '播放方向', ['forward', 'reverse', 'pingpong']),
                'repeat': v.integer(source_clip.get('repeat', 0), '循环次数', 0, 65535),
            })
        project['clips'] = clips
        project['notes'] = '从裁边/旋转精灵图集恢复透明画布、时长、枢轴与碰撞框。'
    return store.get(created['id'])
