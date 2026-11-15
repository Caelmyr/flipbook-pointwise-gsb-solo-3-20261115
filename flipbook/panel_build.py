"""Construction, rasterization and mutation of layered panel workspaces.

The output is a normal editable project whose single frame keeps one layer
per source layer.  Every layer carries both its current rendered asset and a
``panel_source`` reference to the pristine full-canvas original, so a resize
always recomputes from the original instead of resampling the previous
result.  Preview, reopen and image export all call the same rasterizer.
"""
from copy import deepcopy
from PIL import Image, ImageDraw
from . import validate as v
from .model import new_id, new_frame, new_layer
from .layer_nodes import walk_layers
from . import panels


def _tiled_piece(piece, width, height):
    output = Image.new('RGBA', (width, height))
    step_x = max(1, piece.width)
    step_y = max(1, piece.height)
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            clipped = piece
            if x + piece.width > width or y + piece.height > height:
                clipped = piece.crop((0, 0, min(piece.width, width - x), min(piece.height, height - y)))
            output.alpha_composite(clipped, (x, y))
    return output


def rasterize_layer(source_image, mapping, width, height):
    """Map one pristine source layer through the panel grid, independently.

    Fixed cells are copied 1:1, stretch cells are nearest-neighbor resized,
    repeat cells are tiled (edge tiles may be clipped).  The result stays a
    single full-canvas layer, so layers crossing stretch borders remain
    independently editable afterwards.
    """
    result = Image.new('RGBA', (width, height))
    col_source, col_target = mapping['col_source'], mapping['col_target']
    row_source, row_target = mapping['row_source'], mapping['row_target']
    for row_index, row in enumerate(mapping['rows']):
        for column_index, column in enumerate(mapping['columns']):
            box = (col_source[column_index], row_source[row_index],
                   col_source[column_index + 1], row_source[row_index + 1])
            dest = (col_target[column_index], row_target[row_index],
                    col_target[column_index + 1], row_target[row_index + 1])
            cell_w = dest[2] - dest[0]
            cell_h = dest[3] - dest[1]
            if cell_w <= 0 or cell_h <= 0:
                continue
            piece = source_image.crop(box)
            if column['behavior'] == 'fixed' and row['behavior'] == 'fixed':
                pass  # fixed corner ornament: never scaled
            elif column['behavior'] == 'repeat' or row['behavior'] == 'repeat':
                piece = _tiled_piece(piece, cell_w, cell_h)
            else:
                piece = piece.resize((cell_w, cell_h), Image.Resampling.NEAREST)
            result.alpha_composite(piece, dest[:2])
    return result


def _collect_source_layers(store, project, frame_id):
    from .model import find_frame
    frame = find_frame(project, frame_id)
    layers = [layer for layer in walk_layers(frame['layers']) if layer.get('kind') != 'group']
    if not layers:
        raise v.Invalid('源帧没有可用的像素图层')
    if len(layers) > 16:
        raise v.Invalid('面板最多保留16个独立图层')
    collected = []
    for layer in layers:
        image = store.image(project['id'], layer['asset'])
        if image.size != (project['width'], project['height']):
            raise v.Invalid('请先烘焙图层变换后再建立面板')
        collected.append({
            'name': layer['name'],
            'source_asset': layer['asset'],
            'opacity': layer['opacity'],
            'blend': layer['blend'],
            'visible': layer['visible'],
        })
    return collected


def create_workspace(store, project_id, body):
    source = store.get(project_id)
    frame_id = body.get('frame_id') or source['frames'][0]['id']
    from .model import find_frame
    find_frame(source, frame_id)
    width, height = v.dimensions(body)
    source_w, source_h = source['width'], source['height']
    if body.get('grid'):
        grid = {
            'columns': panels._grid_entries(body['grid'].get('columns'), 'x', source_w, '列网格'),
            'rows': panels._grid_entries(body['grid'].get('rows'), 'y', source_h, '行网格'),
        }
    else:
        margins = panels.validate_margins(source_w, source_h, body)
        grid = panels.default_grid(source_w, source_h, margins)
    decorations_raw = v.array(body.get('decorations', []), '装饰', panels.MAX_DECORATIONS)
    known = {item.get('id') for item in decorations_raw if item.get('id')}
    decorations = [panels._decoration(item, known) for item in decorations_raw]
    source_layers = _collect_source_layers(store, source, frame_id)
    workspace = {
        'id': new_id(),
        'name': v.text(body.get('name', '面板作品'), '面板名称', 100, False),
        'frame_id': frame_id,
        'source_project_id': project_id,
        'source_size': [source_w, source_h],
        'grid': grid,
        'decorations': decorations,
        'last_feasible': None,
    }
    # Full structural validation (grid totals, decoration bounds, attachment
    # chains) and feasibility at the requested size before anything is built.
    panels.validate_workspace(workspace)
    layout = panels.solve_layout(workspace, width, height)
    workspace['last_feasible'] = {
        'width': width, 'height': height,
        'decorations': deepcopy(layout['decorations']),
    }

    target = store.create(source['name'][:90] + ' · 面板', width, height)
    target_id = target['id']
    # Copy pristine originals into the new project, then render each layer
    # independently from those copies.
    rendered_layers = []
    for item in source_layers:
        original = store.image(project_id, item['source_asset'])
        pristine_asset = store.put_image(target_id, original)
        rendered = rasterize_layer(original, layout['mapping'], width, height)
        rendered_asset = store.put_image(target_id, rendered)
        layer = new_layer(item['name'], rendered_asset,
                          opacity=item['opacity'], blend=item['blend'], visible=item['visible'])
        layer['panel_source'] = {
            'asset': pristine_asset,
            'workspace_id': workspace['id'],
            'source_name': item['name'],
        }
        rendered_layers.append(layer)
    frame = new_frame(rendered_layers[0]['asset'], '面板帧')
    frame['layers'] = rendered_layers
    frame['panel_workspace'] = deepcopy(workspace)
    with store.edit(target_id, 0, '建立面板工程') as project:
        project['frames'] = [frame]
        project['palette'] = source['palette'][:]
        project['notes'] = '面板工程：从分层原稿重算，固定边饰不拉伸，每次改尺寸都依据原稿。'
    return store.get(target_id)


def resize_workspace(store, project_id, revision, body):
    project = store.get(project_id)
    frame = project['frames'][0]
    workspace = frame.get('panel_workspace')
    if not workspace:
        raise v.Invalid('当前项目不是面板工程')
    width, height = v.dimensions(body)
    edited_layers = [layer['name'] for layer in walk_layers(frame['layers'])
                     if layer.get('panel_diverged')]
    if edited_layers and not v.boolean(body.get('confirm_overwrite', False), '确认覆盖手动编辑'):
        raise v.Invalid('以下图层在面板生成后被手动修改，重算会覆盖：%s；如确认请提交 confirm_overwrite'
                        % '、'.join(edited_layers))
    # Raises Invalid with concrete conflicts and writes nothing on failure,
    # so the last feasible result remains intact.
    layout = panels.solve_layout(workspace, width, height)
    mapping = layout['mapping']
    orphan_layers = [layer['name'] for layer in walk_layers(frame['layers'])
                     if not layer.get('panel_source')]
    if orphan_layers:
        raise v.Invalid('以下图层没有分层原稿，无法从原稿重算：%s；请先将其合并到面板图层或移除后再改尺寸'
                        % '、'.join(orphan_layers))
    with store.edit(project_id, revision, '面板尺寸重算') as working:
        active_frame = working['frames'][0]
        for layer in walk_layers(active_frame['layers']):
            meta = layer.get('panel_source')
            if not meta:
                continue
            original = store.image(project_id, meta['asset'])
            rendered = rasterize_layer(original, mapping, width, height)
            layer['asset'] = store.put_image(project_id, rendered)
            layer.pop('panel_diverged', None)
        working['width'] = width
        working['height'] = height
        active_frame['panel_workspace']['last_feasible'] = {
            'width': width, 'height': height,
            'decorations': deepcopy(layout['decorations']),
        }
    return store.get(project_id)


def update_workspace(store, project_id, revision, body):
    project = store.get(project_id)
    workspace = project['frames'][0].get('panel_workspace')
    if not workspace:
        raise v.Invalid('当前项目不是面板工程')
    updated = deepcopy(workspace)
    if 'grid' in body:
        source_w, source_h = workspace['source_size']
        updated['grid'] = {
            'columns': panels._grid_entries(body['grid'].get('columns'), 'x', source_w, '列网格'),
            'rows': panels._grid_entries(body['grid'].get('rows'), 'y', source_h, '行网格'),
        }
    if 'decorations' in body:
        raw_list = v.array(body['decorations'], '装饰', panels.MAX_DECORATIONS)
        known = {item.get('id') for item in raw_list if item.get('id')}
        updated['decorations'] = [panels._decoration(item, known) for item in raw_list]
    if 'name' in body:
        updated['name'] = v.text(body['name'], '面板名称', 100, False)
    # The new definition must be structurally valid and stay feasible at the
    # current size before anything is saved.
    panels.validate_workspace(updated)
    panels.solve_layout(updated, project['width'], project['height'])
    with store.edit(project_id, revision, '更新面板网格与装饰') as working:
        working['frames'][0]['panel_workspace'] = updated
    return store.get(project_id)


def preview_image(store, project, width=None, height=None):
    workspace = project['frames'][0].get('panel_workspace')
    if not workspace:
        raise v.Invalid('当前项目不是面板工程')
    width = width or project['width']
    height = height or project['height']
    layout = panels.solve_layout(workspace, width, height)
    mapping = layout['mapping']
    from .composite import blend_pixels, transform_layer
    output = Image.new('RGBA', (width, height))
    for layer in walk_layers(project['frames'][0]['layers']):
        meta = layer.get('panel_source')
        if not meta or not layer['visible']:
            continue
        original = store.image(project['id'], meta['asset'])
        rendered = rasterize_layer(original, mapping, width, height)
        rendered = transform_layer(rendered, layer['transform'], (width, height))
        output = blend_pixels(output, rendered, layer['blend'], layer['opacity'])
    draw = ImageDraw.Draw(output)
    for x in mapping['col_target'][1:-1]:
        draw.line((x, 0, x, height - 1), fill=(102, 226, 189, 120))
    for y in mapping['row_target'][1:-1]:
        draw.line((0, y, width - 1, y), fill=(102, 226, 189, 120))
    for rect in layout['decorations'].values():
        x, y, w, h = rect
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(248, 204, 101, 220))
    return output, layout
