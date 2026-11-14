from . import validate as v


def walk_layers(layers, depth=0):
    if depth > 8:
        raise v.Invalid('图层组合过深')
    for layer in layers:
        yield layer
        if layer.get('kind') == 'group':
            children = layer.get('children')
            if not isinstance(children, list) or not children:
                raise v.Invalid('图层组合必须包含子层')
            yield from walk_layers(children, depth + 1)


def require_leaf(layer):
    if layer.get('kind') == 'group':
        raise v.Invalid('请展开组合后编辑其中的像素图层')


def parent_layers(frame, identifier):
    pending = [frame['layers']]
    while pending:
        siblings = pending.pop()
        if any(layer['id'] == identifier for layer in siblings):
            return siblings
        for layer in siblings:
            if layer.get('kind') == 'group':
                pending.append(layer['children'])
    raise v.Invalid('图层不存在')
