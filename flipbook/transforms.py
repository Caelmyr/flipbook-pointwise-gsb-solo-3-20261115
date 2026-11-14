"""Shared affine rasterization used by both flat layers and grouped layers.

A single code path for every layer keeps the pixel result identical before and
after a group is created: a layer transform is always the affine matrix around
the canvas centre, and a group is the product of its own matrix with the
matrices of its descendants.  Resampling happens exactly once per raster layer,
so content outside the visible canvas at an intermediate stage is not clipped
before the enclosing group transform is applied.
"""
import math
import numpy as np
from PIL import Image


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


def canvas_image(image, size):
    """Place the asset on the canvas without changing canvas-sized content.

    Raster layers are normally canvas-sized.  A smaller asset (legacy role
    fallback or an imported layer) is centred, matching how the flat transform
    path has always positioned it.
    """
    if image.size == size:
        return image
    canvas = Image.new('RGBA', size)
    canvas.alpha_composite(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return canvas


def rasterize(image, matrix, size):
    if size[0] * size[1] > 16777216:
        raise ValueError('图层变换后的尺寸过大')
    source = canvas_image(image.convert('RGBA'), size)
    inverse = np.linalg.inv(matrix)
    pixels = np.asarray(source)
    result = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    x = np.arange(size[0], dtype=float)[None, :] + .5
    for start in range(0, size[1], 64):
        end = min(start + 64, size[1])
        y = np.arange(start, end, dtype=float)[:, None] + .5
        sx = np.floor(inverse[0, 0] * x + inverse[0, 1] * y + inverse[0, 2] + 1e-9).astype(int)
        sy = np.floor(inverse[1, 0] * x + inverse[1, 1] * y + inverse[1, 2] + 1e-9).astype(int)
        valid = (sx >= 0) & (sy >= 0) & (sx < source.width) & (sy < source.height)
        result[start:end][valid] = pixels[sy[valid], sx[valid]]
    return Image.fromarray(result)
