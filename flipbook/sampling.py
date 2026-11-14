import numpy as np
from . import validate as v


def sample_pixel(image, body):
    position = v.point(body.get('point'), image.width, image.height)
    x, y = [int(round(value)) for value in position]
    x = min(image.width - 1, x)
    y = min(image.height - 1, y)
    rgba = image.convert('RGBA').getpixel((x, y))
    return {'point': [x, y], 'rgba': list(rgba), 'hex': '#%02x%02x%02x%02x' % rgba}


def sample_region(image, body):
    region = v.rect(body, image.width, image.height)
    x, y = region['x'], region['y']
    raster = np.asarray(image.convert('RGBA'))[y:y + region['height'], x:x + region['width']]
    active = raster[:, :, 3] > 0
    if not np.any(active):
        return {'region': region, 'visible_pixels': 0, 'mean_hex': '#00000000', 'mean_rgba': [0, 0, 0, 0], 'unique_colors': 0}
    colors = raster[active].astype(float)
    weight = colors[:, 3]
    rgb = np.sum(colors[:, :3] * weight[:, None], axis=0) / weight.sum()
    alpha = float(raster[:, :, 3].mean())
    mean = [int(round(value)) for value in [*rgb, alpha]]
    histogram = np.bincount(raster[:, :, 3].ravel(), minlength=256)
    return {
        'region': region,
        'visible_pixels': int(active.sum()),
        'mean_hex': '#%02x%02x%02x%02x' % tuple(mean),
        'mean_rgba': mean,
        'median_visible_rgb': [int(value) for value in np.median(colors[:, :3], axis=0)],
        'unique_colors': int(len(np.unique(raster.reshape(-1, 4), axis=0))),
        'alpha_histogram': histogram.tolist(),
    }


def nearest_saved_color(color, palette):
    source = np.asarray(v.rgba(color), dtype=float)
    validated = v.palette(palette)
    values = np.asarray([v.rgba(item) for item in validated], dtype=float)
    distances = np.linalg.norm(values - source[None, :], axis=1)
    index = int(np.argmin(distances))
    return {'source': v.hex_color(color), 'nearest': validated[index], 'index': index, 'rgba_distance': round(float(distances[index]), 6)}
