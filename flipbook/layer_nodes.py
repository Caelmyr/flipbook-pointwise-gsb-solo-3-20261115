from copy import deepcopy
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


def group_depth(items, depth=0):
    deepest = depth
    for layer in items:
        if layer.get('kind') == 'group':
            deepest = max(deepest, group_depth(layer['children'], depth + 1))
    return deepest


def require_group_editable(layer):
    if layer.get('locked'):
        raise v.Invalid('图层组合已锁定，请先解锁')
    if not layer.get('visible', True):
        raise v.Invalid('请先显示组合再操作')
    if layer.get('mask'):
        raise v.Invalid('带遮罩的组合不能展开或解组')


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


def ancestors(frame, identifier):
    """Enclosing groups of a layer, outermost first."""
    path = []

    def seek(siblings):
        for layer in siblings:
            if layer['id'] == identifier:
                return True
            if layer.get('kind') == 'group' and seek(layer['children']):
                path.append(layer)
                return True
        return False

    if not seek(frame['layers']):
        raise v.Invalid('图层不存在')
    return path[::-1]


def substitute_branch(layers, target_siblings, replacement):
    """Copy a nested layer list with ``target_siblings`` replaced.

    The list objects in a project are plain containers, so comparing identity
    identifies the exact nesting level without relying on layer ids.
    """
    if layers is target_siblings:
        return replacement
    result = []
    for layer in layers:
        if layer.get('kind') == 'group':
            children = substitute_branch(layer['children'], target_siblings, replacement)
            if children is not layer['children']:
                layer = dict(layer, children=children)
        result.append(layer)
    return result


def identity_transform(node):
    return node.get('transform', {}) == v.transform({}) and not node.get('affine')


def require_edit_coordinates(frame, layer):
    """Reject pixel edits whose canvas coordinates would not match the view.

    A child inside a group whose own transform is identity is editable in the
    same coordinate system as the canvas.  Once any enclosing group is moved,
    scaled or rotated, the visible child is resampled by that group matrix;
    the page then asks the user to expand the group first instead of writing
    pixels into the wrong place.
    """
    for group in ancestors(frame, layer['id']):
        if not identity_transform(group):
            raise v.Invalid('组合带有位移、缩放或旋转；请先展开组合再编辑内部图层，保持坐标一致')


def flatten_to_children(group, size):
    """Fold a group's own matrix into its direct children.

    This is the single coordinate operation shared by ``ungroup`` and by the
    ``expand`` (edit-in-place) action.  Each direct child receives
    ``group_world * child_world`` as its retained affine, which is exactly the
    matrix the tree renderer used while the child sat inside the group, so the
    rendered pixels do not move.  Nested groups get the same affine and keep
    their own children, so an entire subtree keeps one coordinate space.
    """
    from .layer_tree import node_matrix
    world = node_matrix(group, size)
    children = []
    for child in group['children']:
        placed = deepcopy(child)
        placed['affine'] = (world @ node_matrix(child, size)).tolist()
        placed['transform'] = v.transform({})
        children.append(placed)
    return children
