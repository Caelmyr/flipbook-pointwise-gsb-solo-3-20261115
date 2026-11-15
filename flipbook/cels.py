"""Shared editable pixel content and independent animation instances.

A shared cel (共用原画) stores one common pixel asset that every member layer
references. Each member keeps its own transform, opacity, blend mode and
visibility. Editing a member must write into the shared source pixels instead
of baking the member's transform into them; otherwise the other members would
inherit the transformed pixels and drift out of place.

Canvas-space strokes are therefore mapped back through the edited member's
display transform to the source pixel grid. The inverse map is produced by
running the exact same raster pipeline as :func:`composite.transform_layer`
over an image that encodes source coordinates, so every visible display
position always resolves to the source pixel it displays.
"""
import numpy as np
from PIL import Image
from . import validate as v
from .model import editable_layer, new_id
from .drawing import (draw, shape_mask, mirrored_paths, apply_paint,
                      erase_pixels)
from .composite import transform_layer

# Source coordinates are packed into RGBA bytes (up to 2048px per side).
# R/B carry the low 8 bits of u/v; G/A carry the high 3 bits plus a marker
# bit (0x80). transform_layer's NEAREST resampling preserves these exact
# values inside the transformed source, while outside pixels are transparent
# (alpha 0), so alpha also flags coverage.
_MARKER = 1 << 7
_HIGH_MASK = 7


def members(project, cel_id):
    return [(frame, layer) for frame in project['frames'] for layer in frame['layers']
            if layer.get('cel_id') == cel_id]


def validate(project):
    sources = {}
    for frame in project['frames']:
        for layer in frame['layers']:
            cel = layer.get('cel_id')
            if cel:
                v.key(cel)
                if cel in sources and sources[cel] != layer['asset']:
                    raise v.Invalid('共用原画成员引用了不同像素')
                sources[cel] = layer['asset']


def link(store, key, revision, source_frame, source_layer, targets):
    with store.edit(key, revision, '建立共用原画') as project:
        _, source = editable_layer(project, source_frame, source_layer)
        selected = [source]
        for target in v.array(targets, '共用原画实例', 300):
            _, layer = editable_layer(project, target['frame_id'], target['layer_id'])
            if layer not in selected:
                selected.append(layer)
        if len(selected) < 2:
            raise v.Invalid('至少选择两个图层实例')
        if any(layer.get('cel_id') for layer in selected):
            raise v.Invalid('请先脱离已有共用原画，再重新选定成员')
        from .role_tools import require_ordinary
        from .layer_nodes import require_leaf
        for item in selected:
            require_ordinary(item)
            require_leaf(item)
        cel_id = new_id()
        for layer in selected:
            layer['cel_id'] = cel_id
            layer['asset'] = source['asset']
    return store.get(key)


def detach(store, key, revision, frame_id, layer_id):
    with store.edit(key, revision, '脱离共用原画') as project:
        _, layer = editable_layer(project, frame_id, layer_id)
        layer.pop('cel_id', None)
    return store.get(key)


def require_detached(layer):
    if layer.get('cel_id'):
        raise v.Invalid('此操作会改变原画像素含义，请先脱离共用原画；其他实例不受影响')


def _coordinate_image(size):
    """Opaque RGBA image encoding each source pixel's own (u, v) coordinate."""
    width, height = size
    yy, xx = np.mgrid[0:height, 0:width]
    encoded = np.empty((height, width, 4), dtype=np.uint8)
    encoded[:, :, 0] = (xx & 255).astype(np.uint8)
    encoded[:, :, 1] = (_MARKER | ((xx >> 8) & _HIGH_MASK)).astype(np.uint8)
    encoded[:, :, 2] = (yy & 255).astype(np.uint8)
    encoded[:, :, 3] = (_MARKER | ((yy >> 8) & _HIGH_MASK)).astype(np.uint8)
    return Image.fromarray(encoded, 'RGBA')


def _decode_coordinates(mapped):
    """Decode the transformed coordinate image into source pixel indices.

    Returns (u, v, valid) arrays aligned with the canvas. Pixels outside the
    transformed source are transparent and therefore invalid.
    """
    array = np.asarray(mapped, dtype=np.uint8)
    u = array[:, :, 0].astype(np.int32) | ((array[:, :, 1] & _HIGH_MASK).astype(np.int32) << 8)
    v = array[:, :, 2].astype(np.int32) | ((array[:, :, 3] & _HIGH_MASK).astype(np.int32) << 8)
    valid = (array[:, :, 1] & _MARKER) > 0
    return u, v, valid


def _source_lookup(transform, source_size, canvas_size):
    """Inverse map: for every canvas pixel, the source pixel it displays."""
    guide = _coordinate_image(source_size)
    guide = transform_layer(guide, transform, canvas_size)
    return _decode_coordinates(guide)


def _identity(transform):
    return transform == v.transform({})


def _canvas_mask(canvas_size, command):
    mask = Image.new('L', canvas_size, 0)
    for path in mirrored_paths(command['points'], canvas_size[0], canvas_size[1],
                               command['mirror_x'], command['mirror_y']):
        path_mask = shape_mask(canvas_size, command, path)
        mask = Image.fromarray(np.maximum(np.asarray(mask), np.asarray(path_mask)))
    return np.asarray(mask) > 0


def _draw_in_source(source, command, lookup):
    u, v, valid = lookup
    width, height = source.size
    covered = _canvas_mask((u.shape[1], u.shape[0]), command)
    covered &= valid
    local = np.zeros((height, width), dtype=bool)
    local[v[covered], u[covered]] = True
    mask = Image.fromarray((local.astype(np.uint8) * 255))
    if command['tool'] == 'erase':
        return erase_pixels(source, mask)
    return apply_paint(source, mask, command['color'])


def draw_shared(store, project, layer, command):
    if command['tool'] not in ('pencil', 'erase', 'line', 'rectangle', 'ellipse', 'polygon'):
        raise v.Invalid('共用原画支持铅笔、橡皮及线条形状；其他像素工具请先脱离共用原画')
    linked = members(project, layer['cel_id'])
    if any(item['locked'] for _, item in linked):
        raise v.Invalid('共用原画中存在锁定实例，请解锁或先脱离')
    if layer.get('affine'):
        raise v.Invalid('该实例带有嵌套变换，请先烘焙或脱离共用原画后再绘制')
    source = store.image(project['id'], layer['asset'])
    if _identity(layer['transform']):
        updated = draw(source, command)
    else:
        lookup = _source_lookup(layer['transform'], source.size,
                                (project['width'], project['height']))
        updated = _draw_in_source(source, command, lookup)
    asset = store.put_image(project['id'], updated)
    # Only the shared pixel reference changes; every member keeps its own
    # transform, opacity, blend mode and visibility.
    for _, instance in linked:
        instance['asset'] = asset


def clear_shared(store, project, layer):
    linked = members(project, layer['cel_id'])
    if any(item['locked'] for _, item in linked):
        raise v.Invalid('共用原画中存在锁定实例')
    source = store.image(project['id'], layer['asset'])
    asset = store.put_image(project['id'], Image.new('RGBA', source.size))
    for _, instance in linked:
        instance['asset'] = asset
