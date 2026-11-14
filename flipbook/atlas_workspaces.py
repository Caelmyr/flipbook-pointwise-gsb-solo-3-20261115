"""Versioned texture-atlas layouts with explicitly pinned sampling regions."""
import io
import json
import zipfile
from copy import deepcopy
from PIL import Image
from . import validate as v
from .model import new_id
from .atlas import MaxRects, Rectangle, prepare_sprites, extrusion
from .composite import png_bytes


def workspaces(project):
    return project.get('atlas_workspaces', [])


def asset_sizes(project):
    return {version['page_asset']: tuple(workspace['size'])
            for workspace in workspaces(project) for version in workspace['versions']}


def find(project, identifier):
    result = next((item for item in workspaces(project) if item['id'] == identifier), None)
    if result is None:
        raise v.Invalid('图集工程不存在')
    return result


def planned_entries(sprites, workspace, locked):
    width, height = workspace['size']
    border, reserve = workspace['padding'] + workspace['extrude'], workspace['reserve']
    prior = workspace['versions'][-1]['entries'] if workspace['versions'] else []
    packer = MaxRects(width, height, workspace['rotate'])
    entries = []
    for index, sprite in enumerate(sprites):
        bounds = sprite['bounds']
        region = [bounds[0]-reserve, bounds[1]-reserve, bounds[2]+reserve, bounds[3]+reserve]
        inserted = packer.insert(region[2]-region[0]+2*border, region[3]-region[1]+2*border)
        if inserted is None:
            raise v.Invalid('当前精灵排列未找到可用布局；旧版本保持可下载')
        rect, rotated = inserted
        entry = {'frame_id': sprite['id'], 'allocation': [rect.x, rect.y, rect.width, rect.height],
                 'region': region, 'rotated': rotated,
                 'uv_pixels': [rect.x+border, rect.y+border, rect.width-2*border, rect.height-2*border]}
        if sprite['id'] in locked and index < len(prior):
            previous = prior[index]
            entry['allocation'] = deepcopy(previous['allocation'])
            entry['uv_pixels'] = deepcopy(previous['uv_pixels'])
            entry['rotated'] = previous['rotated']
        entry.update(name=sprite['name'], source_size=list(sprite['source_size']), source_bounds=bounds,
                     duration=sprite['duration'], pivot=sprite['pivot'], boxes=sprite['boxes'])
        entries.append(entry)
    return entries



def render_page(store, project, workspace, entries):
    from .composite import render_frame
    page = Image.new('RGBA', tuple(workspace['size']))
    for item in entries:
        source = render_frame(store, project, item['frame_id'])
        pixels = source.crop(tuple(item['region']))
        if item['rotated']:
            pixels = pixels.transpose(Image.Transpose.ROTATE_270)
        x, y, width, height = item['uv_pixels']
        if pixels.size != (width, height):
            raise v.Invalid('图块采样区域与槽位尺寸不一致')
        extrusion(page, pixels, x, y, workspace['extrude'])
    return page


def build(store, key, revision, body, identifier=None):
    with store.edit(key, revision, '生成图集工程版本') as project:
        collection = project.setdefault('atlas_workspaces', [])
        if identifier:
            workspace = find(project, identifier)
        else:
            if len(collection) >= 8:
                raise v.Invalid('每项目最多8个图集工程')
            size = [v.integer(body.get('width', 256), '图集宽', 32, 2048), v.integer(body.get('height', 256), '图集高', 32, 2048)]
            workspace = {'id': new_id(), 'name': v.text(body.get('name', '游戏图集'), '工程名称', 100, False),
                         'size': size, 'padding': v.integer(body.get('padding', 1), '间隔', 0, 16),
                         'extrude': v.integer(body.get('extrude', 1), '外扩', 0, 8),
                         'reserve': v.integer(body.get('reserve', 4), '像素预留', 0, 32),
                         'rotate': v.boolean(body.get('rotate', False), '允许旋转'), 'locked': [], 'versions': []}
            collection.append(workspace)
        if len(workspace['versions']) >= 20:
            raise v.Invalid('每工程最多20个版本，请另建工程')
        locked = v.array(body.get('locked', workspace['locked']), '固定图块', 300)
        if len(set(locked)) != len(locked):
            raise v.Invalid('固定图块重复')
        sprites = prepare_sprites(store, project, [frame['id'] for frame in project['frames']], True)
        entries = planned_entries(sprites, workspace, locked)
        page = render_page(store, project, workspace, entries)
        workspace['locked'] = list(locked)
        workspace['versions'].append({'number': len(workspace['versions'])+1, 'source_revision': project['revision'],
                                      'page_asset': store.put_image(key, page), 'entries': entries})
    return store.get(key)


def bundle(store, key, identifier, number):
    project = store.get(key); workspace = find(project, identifier)
    version = next((v for v in workspace['versions'] if v['number'] == number), None)
    if version is None:
        raise v.Invalid('图集版本不存在')
    metadata = {'format': 'flipbook-pinned-atlas-v1', 'name': workspace['name'], 'size': workspace['size'],
                'version': version['number'], 'source_revision': version['source_revision'], 'image': 'atlas.png',
                'padding': workspace['padding'], 'extrude': workspace['extrude'], 'entries': version['entries']}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        archive.writestr('atlas.png', png_bytes(store.image(key, version['page_asset'])))
    return stream.getvalue()
