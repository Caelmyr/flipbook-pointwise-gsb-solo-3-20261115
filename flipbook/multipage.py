import io
import zipfile
from PIL import Image
from . import validate as v
from .atlas import MaxRects, prepare_sprites, extrusion
from .composite import png_bytes
from .exporting import json_bytes


def page_pack(sprites, page_size, padding, extrude, rotate):
    border = padding + extrude
    pages = []
    ordered = sorted(sprites, key=lambda s: (max(s['image'].size), s['image'].width * s['image'].height), reverse=True)
    for sprite in ordered:
        width = sprite['image'].width + border * 2
        height = sprite['image'].height + border * 2
        inserted = None
        page_index = None
        for index, page in enumerate(pages):
            inserted = page['packer'].insert(width, height)
            if inserted is not None:
                page_index = index
                break
        if inserted is None:
            packer = MaxRects(page_size[0], page_size[1], rotate)
            inserted = packer.insert(width, height)
            if inserted is None:
                raise v.Invalid('至少一张精灵无法放入单页图集')
            pages.append({'packer': packer, 'placements': []})
            page_index = len(pages) - 1
            if len(pages) > 32:
                raise v.Invalid('图集超过32页，请增大单页尺寸')
        pages[page_index]['placements'].append((sprite, inserted[0], inserted[1]))
    return pages


def multipage_atlas(store, project, body):
    ids = v.frame_ids(body.get('frame_ids', [frame['id'] for frame in project['frames']]), project)
    width = v.integer(body.get('page_width', 512), '每页宽度', 32, 4096)
    height = v.integer(body.get('page_height', 512), '每页高度', 32, 4096)
    padding = v.integer(body.get('padding', 1), '精灵间隔', 0, 16)
    extrude = v.integer(body.get('extrude', 1), '边缘外扩', 0, 8)
    trim = v.boolean(body.get('trim', True), '透明裁边')
    rotate = v.boolean(body.get('rotate', False), '允许旋转')
    sprites = prepare_sprites(store, project, ids, trim)
    pages = page_pack(sprites, (width, height), padding, extrude, rotate)
    if width * height * len(pages) > 64000000:
        raise v.Invalid('多页图集超过6400万像素预算')
    border = padding + extrude
    manifest = {
        'format': 'flipbook-multipage-atlas-v1',
        'source_project': project['id'],
        'revision': project['revision'],
        'pages': [],
        'frames': [],
        'clips': project['clips'],
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for page_index, page in enumerate(pages):
            image = Image.new('RGBA', (width, height))
            entries = []
            area = 0
            filename = f'atlas-{page_index + 1:02d}.png'
            for sprite, rectangle, rotated in page['placements']:
                raster = sprite['image']
                if rotated:
                    raster = raster.transpose(Image.Transpose.ROTATE_270)
                x, y = rectangle.x + border, rectangle.y + border
                extrusion(image, raster, x, y, extrude)
                area += raster.width * raster.height
                bounds = sprite['bounds']
                entry = {
                    'id': sprite['id'],
                    'index': sprite['index'],
                    'name': sprite['name'],
                    'page': page_index,
                    'image': filename,
                    'frame': {'x': x, 'y': y, 'width': raster.width, 'height': raster.height},
                    'rotated_clockwise': rotated,
                    'source_size': {'width': sprite['source_size'][0], 'height': sprite['source_size'][1]},
                    'sprite_source': {'x': bounds[0], 'y': bounds[1], 'width': bounds[2] - bounds[0], 'height': bounds[3] - bounds[1]},
                    'duration': sprite['duration'],
                    'pivot': sprite['pivot'],
                    'boxes': sprite['boxes'],
                }
                entries.append(entry)
                manifest['frames'].append(entry)
            page_meta = {'index': page_index, 'image': filename, 'width': width, 'height': height, 'sprite_count': len(entries), 'occupancy_percent': round(area / (width * height) * 100, 5)}
            manifest['pages'].append(page_meta)
            archive.writestr(filename, png_bytes(image))
            archive.writestr(f'atlas-{page_index + 1:02d}.json', json_bytes({'page': page_meta, 'frames': entries}))
        manifest['frames'].sort(key=lambda entry: entry['index'])
        archive.writestr('manifest.json', json_bytes(manifest))
        archive.writestr('README.txt', 'Multi-page sprite atlas. Page indices are zero-based.\nframe dimensions describe packed orientation; undo clockwise rotation before restoring sprite_source offset.\n')
    return stream.getvalue(), {'page_count': len(pages), 'sprite_count': len(sprites), 'pages': manifest['pages']}
