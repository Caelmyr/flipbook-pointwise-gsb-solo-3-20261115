"""Editable layer tree geometry and compositing."""
import math
import numpy as np
from PIL import Image
from .composite import blend_pixels


def local_matrix(transform, size):
    angle = math.radians(transform.get('rotation', 0))
    co, si = math.cos(angle), math.sin(angle)
    sx = transform.get('scale_x', 1) * (-1 if transform.get('flip_x') else 1)
    sy = transform.get('scale_y', 1) * (-1 if transform.get('flip_y') else 1)
    if not .0001 <= abs(sx * sy) <= 64:
        raise ValueError('Degenerate or excessive scale')
    cx, cy = size[0] / 2, size[1] / 2
    center = np.array([[1., 0., cx], [0., 1., cy], [0., 0., 1.]])
    origin = np.array([[1., 0., -cx], [0., 1., -cy], [0., 0., 1.]])
    shift = np.array([[1., 0., transform.get('x', 0)],
                      [0., 1., transform.get('y', 0)], [0., 0., 1.]])
    rotation = np.array([[co, -si, 0.], [si, co, 0.], [0., 0., 1.]])
    scale = np.diag([sx, sy, 1.])
    return shift @ center @ rotation @ scale @ origin


def rasterize(image, matrix, size):
    inverse = np.linalg.inv(matrix)
    pixels = np.asarray(image.convert('RGBA'))
    result = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    x = np.arange(size[0], dtype=float)[None, :] + .5
    for start in range(0, size[1], 64):
        end = min(start + 64, size[1])
        y = np.arange(start, end, dtype=float)[:, None] + .5
        sx = np.floor(inverse[0, 0] * x + inverse[0, 1] * y + inverse[0, 2] + 1e-9).astype(int)
        sy = np.floor(inverse[1, 0] * x + inverse[1, 1] * y + inverse[1, 2] + 1e-9).astype(int)
        valid = (sx >= 0) & (sy >= 0) & (sx < image.width) & (sy < image.height)
        result[start:end][valid] = pixels[sy[valid], sx[valid]]
    return Image.fromarray(result)


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
                if node.get('passthrough', True) and node.get('blend', 'normal') != 'normal':
                    raise ValueError('A passthrough group has no additional blend operation')
                walk(node['children'], depth + 1)
            elif 'asset' not in node:
                raise ValueError('Missing raster asset')
            active.remove(identifier)
    walk(nodes, 0)


def render_tree(nodes, size, get_asset, backdrop=None, overrides=None):
    validate_tree(nodes)
    base = backdrop.copy() if backdrop is not None else Image.new('RGBA', size)
    for node in nodes:
        if not node.get('visible', True):
            continue
        if node.get('kind') == 'group':
            image = render_tree(node['children'], size, get_asset, overrides=overrides)
        elif overrides and node['id'] in overrides:
            image = overrides[node['id']]
        else:
            image = get_asset(node['asset'])
        retained = np.asarray(node.get('affine', np.eye(3)), dtype=float)
        world = local_matrix(node.get('transform', {}), size) @ retained
        image = rasterize(image, world, size)
        if node.get('mask'):
            mask = rasterize(get_asset(node['mask']), world, size)
            alpha = np.asarray(image.getchannel('A'), dtype=float) * np.asarray(mask.getchannel('A'), dtype=float) / 255
            image.putalpha(Image.fromarray(np.rint(alpha).astype(np.uint8)))
        base = blend_pixels(base, image, node.get('blend', 'normal'), node.get('opacity', 1))
    return base

