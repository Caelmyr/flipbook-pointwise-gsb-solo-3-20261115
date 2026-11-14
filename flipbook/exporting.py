from .layer_nodes import walk_layers
import csv
import io
import json
import zipfile
import numpy as np
from PIL import Image
from . import validate as v
from .model import find_frame, project_snapshot
from .composite import render_frame, png_bytes
from .atlas import build_atlas, grid_sheet
from .palettes import extract_palette
from .exposures import ordered_exposures


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode('utf8')


def sequence_ids(project, body):
    from .exposures import ordered_exposures
    items, _ = ordered_exposures(project, body.get('clip_id'), body.get('frame_ids'))
    return [item['frame_id'] for item in items]


def rendered_sequence(store, project, body):
    from .exposures import ordered_exposures
    exposures, _ = ordered_exposures(project, body.get('clip_id'), body.get('frame_ids'))
    ids = [item['frame_id'] for item in exposures]
    scale = v.integer(body.get('scale', 1), '输出倍数', 1, 8)
    v.pixel_budget(project['width'] * scale, project['height'] * scale, len(ids), maximum=60000000)
    images = []
    durations = []
    background = v.boolean(body.get('background', False), '使用背景颜色')
    for item in exposures:
        frame_id = item['frame_id']
        image = render_frame(store, project, frame_id, background)
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
        images.append(image)
        durations.append(item['duration'])
    return images, durations, ids


def animated_png(store, project, body):
    images, durations, ids = rendered_sequence(store, project, body)
    stream = io.BytesIO()
    images[0].save(
        stream,
        format='PNG',
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=ordered_exposures(project, body.get('clip_id'), body.get('frame_ids'))[1],
        disposal=0,
        blend=0,
        optimize=False,
    )
    return stream.getvalue(), {'requested_frames': len(ids), 'duration': sum(durations), 'format': 'APNG'}


def animated_webp(store, project, body):
    images, durations, ids = rendered_sequence(store, project, body)
    stream = io.BytesIO()
    images[0].save(stream, format='WEBP', save_all=True, append_images=images[1:], duration=durations, loop=ordered_exposures(project, body.get('clip_id'), body.get('frame_ids'))[1], lossless=True, method=4)
    return stream.getvalue(), {'requested_frames': len(ids), 'duration': sum(durations), 'format': 'WEBP'}


def gif_palette(images, count=255):
    samples = []
    for image in images:
        rgb = np.asarray(image.convert('RGB'))
        alpha = np.asarray(image.getchannel('A'))
        visible = rgb[alpha >= 128]
        if len(visible) > 20000:
            visible = visible[::int(np.ceil(len(visible) / 20000))]
        if len(visible):
            samples.append(visible)
    if not samples:
        palette = [0] * 768
    else:
        values = np.concatenate(samples)
        if len(values) > 500000:
            values = values[::int(np.ceil(len(values) / 500000))]
        quantized = Image.fromarray(values.reshape(1, -1, 3)).quantize(colors=count)
        palette = quantized.getpalette()[:count * 3]
        palette += [0] * (768 - len(palette))
    palette_image = Image.new('P', (1, 1))
    palette_image.putpalette(palette)
    return palette_image


def animated_gif(store, project, body):
    images, durations, ids = rendered_sequence(store, project, body)
    palette = gif_palette(images)
    converted = []
    for image in images:
        indexed = image.convert('RGB').quantize(palette=palette, dither=Image.Dither.NONE)
        array = np.asarray(indexed).copy()
        alpha = np.asarray(image.getchannel('A'))
        reserved = (array == 255) & (alpha >= 128)
        if np.any(reserved):
            from .palettes import nearest_palette
            available = np.asarray(palette.getpalette()[:765], dtype=np.uint8).reshape(255, 3)
            rgb = np.asarray(image.convert('RGB'))
            replacement = nearest_palette(rgb[reserved].reshape(1, -1, 3), available).ravel()
            array[reserved] = replacement
        array[alpha < 128] = 255
        indexed = Image.fromarray(array, mode='P')
        indexed.putpalette(palette.getpalette())
        indexed.info['transparency'] = 255
        converted.append(indexed)
    quantized_durations = [max(20, int(round(value / 10) * 10)) for value in durations]
    stream = io.BytesIO()
    converted[0].save(stream, format='GIF', save_all=True, append_images=converted[1:], duration=quantized_durations, loop=ordered_exposures(project, body.get('clip_id'), body.get('frame_ids'))[1], transparency=255, disposal=2, optimize=False)
    return stream.getvalue(), {
        'requested_frames': len(ids),
        'source_duration': sum(durations),
        'export_duration': sum(quantized_durations),
        'format': 'GIF',
        'warning': 'GIF使用二值透明度和全局255色，时长量化为10毫秒；相同帧可能由编码器合并时长',
    }


def png_sequence(store, project, body):
    images, durations, ids = rendered_sequence(store, project, body)
    stream = io.BytesIO()
    metadata = []
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for index, (image, duration, frame_id) in enumerate(zip(images, durations, ids)):
            name = f'frames/{index + 1:04d}.png'
            archive.writestr(name, png_bytes(image))
            metadata.append({'file': name, 'duration': duration, 'id': frame_id})
        archive.writestr('timeline.json', json_bytes({'frames': metadata, 'loop': ordered_exposures(project, body.get('clip_id'), body.get('frame_ids'))[1]}))
    return stream.getvalue()


def atlas_zip(store, project, body):
    image, metadata = build_atlas(store, project, body)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('atlas.png', png_bytes(image))
        archive.writestr('atlas.json', json_bytes(metadata))
    return stream.getvalue(), metadata


def sheet_zip(store, project, body):
    ids = sequence_ids(project, body)
    image, metadata = grid_sheet(store, project, ids, body.get('columns', 8), body.get('spacing', 0), body.get('margin', 0), body.get('scale', 1))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('sheet.png', png_bytes(image))
        archive.writestr('sheet.json', json_bytes(metadata))
    return stream.getvalue(), metadata


def project_bundle(store, project):
    assets = {asset for frame in project['frames'] for layer in walk_layers(frame['layers'])
              for asset in [layer['asset'], layer.get('role_legacy_asset')] if asset}
    from .atlas_workspaces import asset_sizes
    assets.update(asset_sizes(project))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        snapshot = project_snapshot(project)
        snapshot['revision'] = project['revision']
        archive.writestr('project.json', json_bytes(snapshot))
        for asset in sorted(assets):
            path = store.directory(project['id']) / 'assets' / (asset + '.png')
            archive.write(path, 'assets/' + asset + '.png')
        archive.writestr('README.txt', 'Flipbook Studio editable project v1.\nImport through the project archive button.\nHistory is intentionally not included; current editable layers, clips, palettes and tilemaps are retained.\n')
    return stream.getvalue()


def tiled_bundle(store, tilemaps, project, map_id):
    from .tilemaps import find_map
    from .map_animation import compile_resources, render_resources
    tilemap = find_map(project, map_id)
    document, sources = compile_resources(project, tilemap, tilemaps._base_document(project, tilemap))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('map.tmj', json_bytes(document))
        for filename, frame_id in sources.items():
            image = render_frame(store, project, frame_id).resize((tilemap['tile_width'], tilemap['tile_height']), Image.Resampling.NEAREST)
            archive.writestr(filename, png_bytes(image))
        archive.writestr('preview.png', png_bytes(render_resources(store, project, document, sources, 0)))
    return stream.getvalue()


def timeline_csv(project):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['index', 'id', 'name', 'start_ms', 'duration_ms', 'layers', 'pivot_x', 'pivot_y'])
    elapsed = 0
    for index, frame in enumerate(project['frames']):
        name = frame['name']
        if name.startswith(('=', '+', '-', '@')):
            name = "'" + name
        writer.writerow([index, frame['id'], name, elapsed, frame['duration'], len(frame['layers']), *frame['pivot']])
        elapsed += frame['duration']
    return ('\ufeff' + stream.getvalue()).encode('utf8')


def palette_files(project):
    colors = [v.rgba(value) for value in project['palette']]
    gpl = ['GIMP Palette', 'Name: Flipbook Studio', 'Columns: 8', '#']
    for index, color in enumerate(colors):
        gpl.append(f'{color[0]:3d} {color[1]:3d} {color[2]:3d}\tColor {index + 1}')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('palette.gpl', '\n'.join(gpl) + '\n')
        archive.writestr('palette.json', json_bytes(project['palette']))
        swatch = Image.new('RGBA', (len(colors) * 24, 24))
        for index, color in enumerate(colors):
            swatch.paste(color, (index * 24, 0, (index + 1) * 24, 24))
        archive.writestr('palette.png', png_bytes(swatch))
    return stream.getvalue()
