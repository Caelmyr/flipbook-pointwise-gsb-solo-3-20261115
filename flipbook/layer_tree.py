"""Editable layer tree geometry and compositing.

A group is a pure container, not a baked image.  Its transform (and any
retained ``affine``) is folded into the world matrix of every descendant, so a
child is sampled exactly once from its own pixels.  Two consequences matter:

* Pass-through groups blend each child against the real running backdrop,
  including layers below the group.  Multiply/screen therefore keep the same
  external background dependency they had before the layers were collapsed.
* Nothing is rasterized onto the canvas at an intermediate stage, so content
  that leaves the visible area at one nesting level is not clipped before an
  enclosing group transform brings it back into view.

Folding is also exactly what ``ungroup`` writes onto the children, which makes
moving, scaling, rotating, expanding and ungrouping keep one coordinate space.
"""
import math
import numpy as np
from PIL import Image
from .composite import blend_pixels
from .transforms import local_matrix, rasterize

__all__ = ['local_matrix', 'rasterize', 'node_matrix', 'premultiplied_mix',
           'validate_tree', 'passthrough_group', 'render_tree']


def node_affine(node):
    return np.asarray(node.get('affine', np.eye(3)), dtype=float)


def node_matrix(node, size):
    """Matrix of one node relative to the context it is rendered in."""
    return local_matrix(node.get('transform', {}), size) @ node_affine(node)


def premultiplied_mix(before, after, weight):
    first = np.asarray(before.convert('RGBA'), dtype=float) / 255
    second = np.asarray(after.convert('RGBA'), dtype=float) / 255
    w = np.asarray(weight, dtype=float)
    if w.ndim == 2:
        w = w[:, :, None]
    alpha = first[:, :, 3:4] * (1 - w) + second[:, :, 3:4] * w
    colors = (first[:, :, :3] * first[:, :, 3:4] * (1 - w)
              + second[:, :, :3] * second[:, :, 3:4] * w)
    colors = np.divide(colors, alpha, out=np.zeros_like(colors), where=alpha > 1e-10)
    return Image.fromarray(np.rint(np.clip(np.concatenate([colors, alpha], axis=2), 0, 1) * 255).astype(np.uint8))


def validate_tree(nodes, budget=256, max_depth=8):
    seen = set()
    active = set()

    def walk(items, depth):
        if depth > max_depth or not isinstance(items, list):
            raise ValueError('Invalid layer depth or children')
        for node in items:
            identifier = node['id']
            if identifier in active:
                raise ValueError('Layer cycle')
            if identifier in seen:
                raise ValueError('A layer instance has multiple parents')
            seen.add(identifier)
            active.add(identifier)
            if len(seen) > budget:
                raise ValueError('Layer budget exceeded')
            if not 0 <= node.get('opacity', 1) <= 1:
                raise ValueError('Invalid opacity')
            if node.get('kind') == 'group':
                if not isinstance(node.get('children'), list) or not node['children']:
                    raise ValueError('Missing group children')
                walk(node['children'], depth + 1)
            elif 'asset' not in node:
                raise ValueError('Missing raster asset')
            active.remove(identifier)
    walk(nodes, 0)


def passthrough_group(node):
    """True when a group can blend child-by-child onto the outer backdrop.

    A pass-through group must not need isolation: opacity, an extra blend mode
    or a mask all require compositing the children together first.  Visibility
    is not part of this test: an invisible group is simply skipped, while its
    children keep their external background dependency when it is visible.
    """
    return (node.get('passthrough', True)
            and node.get('blend', 'normal') == 'normal'
            and node.get('opacity', 1) == 1
            and not node.get('mask'))


def render_tree(nodes, size, get_asset, backdrop=None, resolve=None):
    validate_tree(nodes)
    base = backdrop.copy() if backdrop is not None else Image.new('RGBA', size)
    resolve = resolve or (lambda layer: get_asset(layer['asset']))
    return _render_nodes(nodes, size, get_asset, base, np.eye(3), resolve)


def _render_nodes(nodes, size, get_asset, base, parent, resolve):
    for node in nodes:
        if not node.get('visible', True):
            continue
        world = parent @ node_matrix(node, size)
        if node.get('kind') == 'group':
            if passthrough_group(node):
                # Every descendant draws straight onto the running backdrop, so
                # blend modes still see the real external background.
                base = _render_nodes(node['children'], size, get_asset, base, world, resolve)
            else:
                # Isolated group: children composite among themselves on a
                # transparent buffer.  Matrices already contain the group
                # transform, so nothing is sampled or clipped twice.
                layer = _render_nodes(node['children'], size, get_asset,
                                      Image.new('RGBA', size), world, resolve)
                if node.get('mask'):
                    layer = _apply_mask(layer, get_asset(node['mask']), world, size)
                base = blend_pixels(base, layer, node.get('blend', 'normal'), node.get('opacity', 1))
            continue
        image = _leaf_image(resolve(node), node, world, size, get_asset)
        base = blend_pixels(base, image, node.get('blend', 'normal'), node.get('opacity', 1))
    return base


def render_node_preview(node, size, get_asset, resolve=None):
    """Render one group's children on transparency for a layer thumbnail.

    Groups have no baked bitmap; the preview shows their live children so the
    layer panel never presents a stale or flattened picture of the group.
    """
    resolve = resolve or (lambda layer: get_asset(layer['asset']))
    if node.get('kind') != 'group':
        return resolve(node)
    base = Image.new('RGBA', size)
    for child in node['children']:
        if not child.get('visible', True):
            continue
        world = node_matrix(child, size)
        if child.get('kind') == 'group':
            image = render_tree([child], size, get_asset, Image.new('RGBA', size), resolve)
        else:
            image = rasterize(resolve(child), world, size)
            if child.get('mask'):
                image = _apply_mask(image, get_asset(child['mask']), world, size)
        base = blend_pixels(base, image, child.get('blend', 'normal'), child.get('opacity', 1))
    return base


def _leaf_image(image, node, world, size, get_asset):
    # A leaf is sampled exactly once, through its full world matrix, with the
    # same rasterizer the flat frame path uses for top-level layers.
    image = rasterize(image, world, size)
    if node.get('mask'):
        image = _apply_mask(image, get_asset(node['mask']), world, size)
    return image


def _apply_mask(image, mask_image, world, size):
    mask = rasterize(mask_image, world, size)
    alpha = np.asarray(image.getchannel('A'), dtype=float) * np.asarray(mask.getchannel('A'), dtype=float) / 255
    image.putalpha(Image.fromarray(np.rint(alpha).astype(np.uint8)))
    return image
