"""Versioned texture-atlas layouts with explicitly pinned sampling regions.

A pinned tile keeps the page slot it had in the last published version *and*
the source sampling rectangle that filled it. Pinning is matched by frame id,
never by timeline position, so reordering frames cannot hand a pinned slot to a
different sprite. Unpinned sprites are packed only into space that remains free
after every pinned allocation has been reserved.
"""
import hashlib
import io
import json
import zipfile
from PIL import Image
from . import validate as v
from .model import new_id
from .atlas import MaxRects, Rectangle, prepare_sprites, extrusion
from .composite import difference, png_bytes


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


def validate_workspaces(project):
    identifiers = set()
    for workspace in v.array(project.get('atlas_workspaces', []), '图集工程', 8):
        identifier = v.key(workspace.get('id'))
        if identifier in identifiers:
            raise v.Invalid('图集工程编号重复')
        identifiers.add(identifier)
        size = v.array(workspace.get('size'), '图集尺寸', 2)
        if len(size) != 2:
            raise v.Invalid('图集尺寸需要宽和高')
        for value in size:
            v.integer(value, '图集边长', 32, 2048)
        locked = v.array(workspace.get('locked', []), '固定图块', 300)
        if len(set(locked)) != len(locked):
            raise v.Invalid('固定图块记录重复')
        for key in locked:
            v.key(key)
        for number, version in enumerate(v.array(workspace.get('versions', []), '图集版本', 20), 1):
            if version.get('number') != number:
                raise v.Invalid('图集版本编号不连续')
            v.digest(version.get('page_asset'))
            if version.get('page_sha256') is not None:
                v.digest(version['page_sha256'])
            for entry in v.array(version.get('entries', []), '图块条目', 300):
                v.key(entry.get('frame_id'))
                for field in ('allocation', 'uv_pixels', 'region'):
                    if len(v.array(entry.get(field), '图块矩形', 4)) != 4:
                        raise v.Invalid('图块矩形需要四个分量')
    return project


def latest_entries(workspace):
    return workspace['versions'][-1]['entries'] if workspace['versions'] else []


def _region_size(region):
    return region[2] - region[0], region[3] - region[1]


def _box(rectangle):
    x, y, width, height = rectangle
    return x, y, x + width, y + height


def _checked_pin(workspace, previous, sprite):
    """Reuse a published slot verbatim and verify the new artwork still fits it."""
    allocation = [int(value) for value in previous['allocation']]
    uv = [int(value) for value in previous['uv_pixels']]
    region = [int(value) for value in previous['region']]
    rotated = bool(previous['rotated'])
    border = workspace['padding'] + workspace['extrude']
    page_width, page_height = workspace['size']
    name = sprite['name']
    valid = (len(allocation) == 4 and len(uv) == 4 and len(region) == 4
             and uv[2] > 0 and uv[3] > 0
             and uv[0] == allocation[0] + border and uv[1] == allocation[1] + border
             and allocation[2] == uv[2] + 2 * border and allocation[3] == uv[3] + 2 * border)
    expected = (_region_size(region)[1], _region_size(region)[0]) if rotated else _region_size(region)
    valid = valid and expected == (uv[2], uv[3])
    valid = valid and allocation[0] >= 0 and allocation[1] >= 0 and allocation[0] + allocation[2] <= page_width and allocation[1] + allocation[3] <= page_height
    if not valid:
        raise v.Invalid(f'固定图块“{name}”的上次发布记录与当前槽位规则不一致，请取消固定后重新生成一版')
    prior_size = previous.get('source_size')
    if prior_size is not None and tuple(prior_size) != tuple(sprite['source_size']):
        raise v.Invalid(
            f'固定图块“{name}”的源画布尺寸已从 {prior_size[0]}×{prior_size[1]} 变为 '
            f'{sprite["source_size"][0]}×{sprite["source_size"][1]}，旧采样区域坐标不再有效；'
            '请取消该图块固定重新排列，或恢复画布尺寸后重试。上次可用版本仍保留可下载')
    if not sprite['empty']:
        x0, y0, x1, y1 = sprite['bounds']
        if x0 < region[0] or y0 < region[1] or x1 > region[0] + region[2] or y1 > region[1] + region[3]:
            raise v.Invalid(
                f'固定图块“{name}”的可见像素超出上次发布的采样区域 '
                f'[{region[0]}, {region[1]}, {region[2]}, {region[3]}]，无法在保持槽位的前提下更新；'
                '请取消该图块固定重新排列，或把补画移回预留范围后重试。上次可用版本仍保留可下载')
    return {'allocation': allocation, 'region': region, 'rotated': rotated, 'uv_pixels': uv}


def planned_entries(sprites, workspace, locked):
    width, height = workspace['size']
    border, reserve = workspace['padding'] + workspace['extrude'], workspace['reserve']
    prior = {entry['frame_id']: entry for entry in latest_entries(workspace)}
    sprite_ids = {sprite['id'] for sprite in sprites}
    pins = {}
    for identifier in locked:
        if identifier not in sprite_ids:
            continue
        previous = prior.get(identifier)
        if previous is not None:
            sprite = next(item for item in sprites if item['id'] == identifier)
            pins[identifier] = _checked_pin(workspace, previous, sprite)
    packer = MaxRects(width, height, workspace['rotate'])
    for identifier in locked:
        if identifier not in pins:
            continue
        allocation = pins[identifier]['allocation']
        if not packer.reserve(Rectangle(allocation[0], allocation[1], allocation[2], allocation[3])):
            name = next(sprite['name'] for sprite in sprites if sprite['id'] == identifier)
            raise v.Invalid(f'固定图块“{name}”的已发布槽位超出画布或与其他固定槽位重叠，无法保留；请取消固定后重试。上次可用版本仍保留可下载')
    entries = []
    for sprite in sprites:
        entry = pins.get(sprite['id'])
        if entry is not None:
            entry = {'frame_id': sprite['id'], **entry}
        else:
            bounds = sprite['bounds']
            region = [bounds[0] - reserve, bounds[1] - reserve, bounds[2] + reserve, bounds[3] + reserve]
            inserted = packer.insert(region[2] - region[0] + 2 * border, region[3] - region[1] + 2 * border)
            if inserted is None:
                raise v.Invalid(f'“{sprite["name"]}”在当前固定槽位之外没有真正可用的空间；请取消部分固定、减少帧或扩大图集。上次可用版本仍保留可下载')
            rect, rotated = inserted
            entry = {'frame_id': sprite['id'], 'allocation': [rect.x, rect.y, rect.width, rect.height],
                     'region': region, 'rotated': rotated,
                     'uv_pixels': [rect.x + border, rect.y + border, rect.width - 2 * border, rect.height - 2 * border]}
        entry.update(name=sprite['name'], source_size=list(sprite['source_size']), source_bounds=sprite['bounds'],
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


def page_digest(store, key, asset):
    path = store.directory(key) / 'assets' / (asset + '.png')
    if not path.exists():
        raise v.Invalid('图集像素文件缺失')
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        locked = [v.key(identifier) for identifier in locked]
        if len(set(locked)) != len(locked):
            raise v.Invalid('固定图块重复')
        sprites = prepare_sprites(store, project, [frame['id'] for frame in project['frames']], True)
        entries = planned_entries(sprites, workspace, locked)
        page = render_page(store, project, workspace, entries)
        asset = store.put_image(key, page)
        sprite_ids = {sprite['id'] for sprite in sprites}
        workspace['locked'] = [identifier for identifier in locked if identifier in sprite_ids]
        workspace['versions'].append({'number': len(workspace['versions']) + 1, 'source_revision': project['revision'],
                                      'page_asset': asset, 'page_sha256': page_digest(store, key, asset),
                                      'entries': entries})
    return store.get(key)


def _version(workspace, number):
    version = next((item for item in workspace['versions'] if item['number'] == number), None)
    if version is None:
        raise v.Invalid('图集版本不存在')
    return version


def _entry_map(entries):
    return {entry['frame_id']: entry for entry in entries}


def compare(store, key, identifier, first_number, second_number):
    project = store.get(key)
    workspace = find(project, identifier)
    first, second = _version(workspace, first_number), _version(workspace, second_number)
    first_image = store.image(key, first['page_asset'])
    second_image = store.image(key, second['page_asset'])
    if first_image.size != second_image.size:
        raise v.Invalid('两个版本的图集尺寸不同，无法逐像素比较')
    diff_image, metrics = difference(first_image, second_image)
    first_entries = _entry_map(first['entries'])
    second_entries = _entry_map(second['entries'])
    rows = []
    for frame_id in [entry['frame_id'] for entry in second['entries']] + [entry['frame_id'] for entry in first['entries'] if entry['frame_id'] not in second_entries]:
        before, after = first_entries.get(frame_id), second_entries.get(frame_id)
        pixel_identical = None
        if before is not None and after is not None:
            a = first_image.crop(_box(before['uv_pixels']))
            b = second_image.crop(_box(after['uv_pixels']))
            pixel_identical = a.size == b.size and a.tobytes() == b.tobytes()
        rows.append({
            'frame_id': frame_id,
            'name': (after or before)['name'],
            'first_slot': before['uv_pixels'] if before else None,
            'second_slot': after['uv_pixels'] if after else None,
            'slot_changed': before is not None and after is not None and before['uv_pixels'] != after['uv_pixels'],
            'first_region': before['region'] if before else None,
            'second_region': after['region'] if after else None,
            'region_changed': before is not None and after is not None and before['region'] != after['region'],
            'first_rotated': before['rotated'] if before else None,
            'second_rotated': after['rotated'] if after else None,
            'pixels_identical': pixel_identical,
        })
    return diff_image, {
        'first': first_number, 'second': second_number,
        'first_digest': first.get('page_sha256'), 'second_digest': second.get('page_sha256'),
        'image_identical': first_image.tobytes() == second_image.tobytes(),
        'changed_pixels': metrics['changed_pixels'],
        'changed_percent': metrics['changed_percent'],
        'mean_absolute_rgba_difference': metrics['mean_absolute_rgba_difference'],
        'entries': rows,
        'moved_frames': [row['name'] for row in rows if row['slot_changed']],
        'resampled_frames': [row['name'] for row in rows if row['region_changed']],
    }


def bundle(store, key, identifier, number):
    project = store.get(key)
    workspace = find(project, identifier)
    version = _version(workspace, number)
    path = store.directory(key) / 'assets' / (version['page_asset'] + '.png')
    if not path.exists():
        raise v.Invalid('图集版本像素文件缺失')
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {'format': 'flipbook-pinned-atlas-v1', 'name': workspace['name'], 'size': workspace['size'],
                'version': version['number'], 'source_revision': version['source_revision'], 'image': 'atlas.png',
                'image_sha256': digest,
                'published_image_sha256': version.get('page_sha256'),
                'padding': workspace['padding'], 'extrude': workspace['extrude'],
                'reserve': workspace['reserve'], 'entries': version['entries']}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        archive.writestr('atlas.png', payload)
    return stream.getvalue()
