"""Shared editable pixel content and independent animation instances."""
import numpy as np
from PIL import Image, ImageOps
from . import validate as v
from .model import editable_layer, new_id
from .drawing import draw, apply_paint, erase_pixels


def members(project, cel_id):
    from .layer_nodes import walk_layers
    return [(frame, layer) for frame in project['frames'] for layer in walk_layers(frame['layers'])
            if layer.get('cel_id') == cel_id]


def validate(project):
    from .layer_nodes import walk_layers
    sources = {}
    for frame in project['frames']:
        for layer in walk_layers(frame['layers']):
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


def draw_shared(store, project, layer, command):
    from .composite import transform_layer
    if command['tool'] not in ('pencil', 'erase', 'line', 'rectangle', 'ellipse', 'polygon'):
        raise v.Invalid('共用原画支持铅笔、橡皮及线条形状；其他像素工具请先脱离共用原画')
    linked = members(project, layer['cel_id'])
    if any(item['locked'] for _, item in linked):
        raise v.Invalid('共用原画中存在锁定实例，请解锁或先脱离')
    # Reuse the ordinary baked-pixel editing route for the selected view.
    image = store.image(project['id'], layer['asset'])
    image = transform_layer(image, layer['transform'], (project['width'], project['height']))
    image = draw(image, command)
    asset = store.put_image(project['id'], image)
    layer['transform'] = v.transform({})
    for _, instance in linked:
        instance['asset'] = asset


def clear_shared(store, project, layer):
    linked = members(project, layer['cel_id'])
    if any(item['locked'] for _, item in linked):
        raise v.Invalid('共用原画中存在锁定实例')
    asset = store.put_image(project['id'], Image.new('RGBA', (project['width'], project['height'])))
    for _, instance in linked:
        instance['asset'] = asset
