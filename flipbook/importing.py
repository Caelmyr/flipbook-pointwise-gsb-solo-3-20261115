from .layer_nodes import walk_layers
import numpy as np
import io
import json
import zipfile
from pathlib import PurePosixPath
from PIL import Image, ImageOps, UnidentifiedImageError
from . import validate as v
from .model import new_frame, new_layer, new_id, validate_structure


def decode_sequence(stream, maximum=300):
    frames = []
    try:
        with Image.open(stream) as image:
            width, height = image.size
            if width > 2048 or height > 2048 or min(width, height) < 8:
                raise v.Invalid('导入尺寸须在8至2048像素之间')
            count = getattr(image, 'n_frames', 1)
            if count > maximum:
                raise v.Invalid('导入序列超过帧数限制')
            v.pixel_budget(width, height, count)
            for index in range(count):
                image.seek(index)
                raster = ImageOps.exif_transpose(image).convert('RGBA')
                duration = max(20, min(10000, int(image.info.get('duration', 100) or 100)))
                frames.append({'image': raster.copy(), 'duration': duration, 'index': index})
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise v.Invalid('无法解码图像，支持PNG/APNG/GIF/WebP/JPEG') from error
    return frames


def fit_canvas(image, size, mode='center'):
    if mode == 'stretch':
        return image.resize(size, Image.Resampling.NEAREST)
    if mode == 'contain':
        image = ImageOps.contain(image, size, Image.Resampling.NEAREST)
    output = Image.new('RGBA', size)
    output.alpha_composite(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return output


def import_into_project(store, project_id, revision, files, mode='append', fit='center', frame_id=None):
    v.choice(mode, '导入方式', ['append', 'layers'])
    v.choice(fit, '画布适配', ['center', 'stretch', 'contain'])
    if len(files) > 30:
        raise v.Invalid('每次最多导入30个文件')
    decoded = []
    for filename, stream in files:
        sequence = decode_sequence(stream)
        decoded.extend((filename, item) for item in sequence)
        if len(decoded) > 300:
            raise v.Invalid('合计超过300帧')
    if not decoded:
        raise v.Invalid('没有可导入的图像')
    with store.edit(project_id, revision, '导入图像序列') as project:
        if mode == 'append' and len(project['frames']) + len(decoded) > 300:
            raise v.Invalid('导入后超过300帧')
        target = None
        if mode == 'layers':
            from .model import find_frame
            target = find_frame(project, frame_id)
            if len(target['layers']) + len(decoded) > 16:
                raise v.Invalid('导入后每帧图层超过16个')
        for filename, item in decoded:
            image = fit_canvas(item['image'], (project['width'], project['height']), fit)
            asset = store.put_image(project_id, image)
            name = PurePosixPath(filename).name[:80] + f' #{item["index"] + 1}'
            if target is None:
                project['frames'].append(new_frame(asset, name, item['duration']))
            else:
                target['layers'].append(new_layer(name, asset))
    return store.get(project_id)


def import_new_project(store, filename, stream):
    decoded = decode_sequence(stream)
    width, height = decoded[0]['image'].size
    project = store.create(PurePosixPath(filename).stem[:100] or '导入动画', width, height)
    try:
        with store.edit(project['id'], 0, '创建导入动画') as working:
            working['frames'] = []
            for item in decoded:
                image = fit_canvas(item['image'], (width, height))
                asset = store.put_image(project['id'], image)
                working['frames'].append(new_frame(asset, f'帧 {item["index"] + 1}', item['duration']))
    except Exception:
        raise
    return store.get(project['id'])


def slice_sheet(store, project_id, revision, stream, body):
    decoded = decode_sequence(stream, 1)
    image = decoded[0]['image']
    cell_w = v.integer(body.get('cell_width', 32), '单元宽度', 8, 2048)
    cell_h = v.integer(body.get('cell_height', 32), '单元高度', 8, 2048)
    spacing = v.integer(body.get('spacing', 0), '单元间隔', 0, 100)
    margin = v.integer(body.get('margin', 0), '图边距', 0, 500)
    skip_empty = v.boolean(body.get('skip_empty', True), '跳过空白')
    fit = v.choice(body.get('fit', 'center'), '画布适配', ['center', 'stretch', 'contain'])
    cells = []
    row = 0
    y = margin
    while y + cell_h <= image.height - margin:
        column = 0
        x = margin
        while x + cell_w <= image.width - margin:
            cropped = image.crop((x, y, x + cell_w, y + cell_h))
            if not skip_empty or cropped.getchannel('A').getbbox():
                cells.append((cropped, row, column))
            if len(cells) > 300:
                raise v.Invalid('切片超过300帧')
            column += 1
            x += cell_w + spacing
        row += 1
        y += cell_h + spacing
    if not cells:
        raise v.Invalid('没有符合尺寸的非空切片')
    with store.edit(project_id, revision, '精灵表切片') as project:
        if len(project['frames']) + len(cells) > 300:
            raise v.Invalid('切片后超过300帧')
        for image, row, column in cells:
            fitted = fit_canvas(image, (project['width'], project['height']), fit)
            project['frames'].append(new_frame(store.put_image(project_id, fitted), f'切片 {row + 1}-{column + 1}'))
    return store.get(project_id)


def safe_zip(archive):
    if len(archive.infolist()) > 1000:
        raise v.Invalid('归档文件过多')
    total = 0
    seen = set()
    for item in archive.infolist():
        name = item.filename
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
            raise v.Invalid('归档路径无效')
        if name in seen:
            raise v.Invalid('归档路径重复')
        seen.add(name)
        if item.is_dir():
            continue
        if (item.external_attr >> 16) & 0o170000 == 0o120000:
            raise v.Invalid('归档不能含符号链接')
        total += item.file_size
        if item.file_size > 20000000 or total > 100000000:
            raise v.Invalid('归档展开体积超限')
        if item.file_size > max(1, item.compress_size) * 5000:
            raise v.Invalid('归档压缩比过高')
    return seen


def import_project_bundle(store, stream):
    try:
        archive = zipfile.ZipFile(stream)
    except zipfile.BadZipFile as error:
        raise v.Invalid('不是有效项目ZIP') from error
    with archive:
        names = safe_zip(archive)
        if 'project.json' not in names:
            raise v.Invalid('缺少project.json')
        try:
            original = json.loads(archive.read('project.json'))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise v.Invalid('项目JSON无效') from error
        validate_structure(original)
        assets = {asset for frame in original['frames'] for layer in walk_layers(frame['layers'])
                  for asset in [layer['asset'], layer.get('role_legacy_asset'), layer.get('mask')] if asset}
        from .atlas_workspaces import asset_sizes, workspaces
        page_sizes = asset_sizes(original)
        assets.update(page_sizes)
        verified = {}
        for asset in assets:
            source = 'assets/' + v.digest(asset) + '.png'
            if source not in names:
                raise v.Invalid('归档缺少图层像素')
            sequence = decode_sequence(io.BytesIO(archive.read(source)), 1)
            image = sequence[0]['image']
            if image.size != page_sizes.get(asset, (original['width'], original['height'])):
                raise v.Invalid('图层尺寸与画布不一致')
            verified[asset] = image
        created = store.create(original['name'][:100] + ' · 导入', original['width'], original['height'])
        with store.edit(created['id'], 0, '恢复项目归档') as project:
            for key, value in original.items():
                if key not in ('id', 'revision', 'undo', 'redo', 'created_at', 'updated_at'):
                    project[key] = value
            project['archived'] = False
            mapping = {asset: store.put_image(created['id'], image) for asset, image in verified.items()}
            for frame in project['frames']:
                for layer in walk_layers(frame['layers']):
                    layer['asset'] = mapping[layer['asset']]
                    if layer.get('role_legacy_asset'):
                        layer['role_legacy_asset'] = mapping[layer['role_legacy_asset']]
                    if layer.get('mask'):
                        layer['mask'] = mapping[layer['mask']]
            for workspace in workspaces(project):
                for version in workspace['versions']:
                    version['page_asset'] = mapping[version['page_asset']]
        return store.get(created['id'])
