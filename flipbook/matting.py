import numpy as np
from PIL import Image, ImageFilter
from . import validate as v
from .model import editable_layer
from .composite import render_frame


def remove_matte(image, color, tolerance=20, softness=10, despill=0):
    array = np.asarray(image.convert('RGBA'), dtype=float)
    rgb = array[:, :, :3]
    target = np.asarray(color[:3], dtype=float)
    distances = np.linalg.norm(rgb - target, axis=2) / np.sqrt(3)
    if softness:
        factor = np.clip((distances - tolerance) / softness, 0, 1)
    else:
        factor = (distances > tolerance).astype(float)
    array[:, :, 3] *= factor
    if despill:
        dominant = int(np.argmax(target))
        other = [index for index in range(3) if index != dominant]
        neutral = np.mean(rgb[:, :, other], axis=2)
        excess = np.maximum(rgb[:, :, dominant] - neutral, 0)
        array[:, :, dominant] -= excess * despill * (1 - factor)
    array[array[:, :, 3] == 0] = 0
    return Image.fromarray(np.rint(np.clip(array, 0, 255)).astype(np.uint8))


def channel_alpha(image, channel='luminance', invert=False, combine='multiply'):
    array = np.asarray(image.convert('RGBA')).copy()
    rgb = array[:, :, :3].astype(float)
    if channel == 'luminance':
        alpha = rgb[:, :, 0] * .2126 + rgb[:, :, 1] * .7152 + rgb[:, :, 2] * .0722
    elif channel in ('red', 'green', 'blue'):
        alpha = rgb[:, :, {'red': 0, 'green': 1, 'blue': 2}[channel]]
    elif channel == 'maximum':
        alpha = rgb.max(axis=2)
    else:
        raise v.Invalid('Alpha来源通道不支持')
    if invert:
        alpha = 255 - alpha
    if combine == 'multiply':
        alpha *= array[:, :, 3] / 255
    elif combine != 'replace':
        raise v.Invalid('透明度组合方式不支持')
    array[:, :, 3] = np.rint(alpha).astype(np.uint8)
    return Image.fromarray(array)


def apply_alpha_mask(image, mask, channel='alpha', invert=False, mode='multiply'):
    if image.size != mask.size:
        raise v.Invalid('遮罩尺寸必须与图层一致')
    array = np.asarray(image.convert('RGBA')).copy()
    if channel == 'alpha':
        source = np.asarray(mask.convert('RGBA'))[:, :, 3].astype(float)
    else:
        source = np.asarray(mask.convert('L'), dtype=float)
    if invert:
        source = 255 - source
    original = array[:, :, 3].astype(float)
    if mode == 'multiply':
        result = original * source / 255
    elif mode == 'subtract':
        result = np.maximum(original - source, 0)
    elif mode == 'minimum':
        result = np.minimum(original, source)
    elif mode == 'maximum':
        result = np.maximum(original, source)
    elif mode == 'replace':
        result = source
    else:
        raise v.Invalid('遮罩组合方式不支持')
    array[:, :, 3] = np.rint(result).astype(np.uint8)
    return Image.fromarray(array)


def refine_alpha(image, operation, radius=1, threshold=128):
    array = np.asarray(image).copy()
    alpha = image.getchannel('A')
    if operation == 'expand':
        alpha = alpha.filter(ImageFilter.MaxFilter(radius * 2 + 1))
    elif operation == 'contract':
        alpha = alpha.filter(ImageFilter.MinFilter(radius * 2 + 1))
    elif operation == 'feather':
        alpha = alpha.filter(ImageFilter.GaussianBlur(radius))
    elif operation == 'threshold':
        alpha = alpha.point(lambda value: 255 if value >= threshold else 0)
    elif operation == 'invert':
        alpha = alpha.point(lambda value: 255 - value)
    else:
        raise v.Invalid('透明度细修操作不支持')
    array[:, :, 3] = np.asarray(alpha)
    return Image.fromarray(array)


def matte_layers(store, project_id, revision, body):
    operation = v.choice(body.get('operation', 'chroma'), '遮罩操作', ['chroma', 'channel', 'frame', 'expand', 'contract', 'feather', 'threshold', 'invert'])
    with store.edit(project_id, revision, '透明度与遮罩编辑') as project:
        _, layer = editable_layer(project, body.get('frame_id'), body.get('layer_id'))
        if layer['transform'] != v.transform({}):
            raise v.Invalid('编辑遮罩前请先烘焙图层变换')
        from .role_tools import require_ordinary
        require_ordinary(layer)
        from .cels import require_detached
        require_detached(layer)
        image = store.image(project_id, layer['asset'])
        if operation == 'chroma':
            color = v.rgba(body.get('color', '#00ff00ff'))
            tolerance = v.real(body.get('tolerance', 20), '色键容差', 0, 255)
            softness = v.real(body.get('softness', 10), '过渡宽度', 0, 100)
            despill = v.real(body.get('despill', 0), '去溢色强度', 0, 1)
            image = remove_matte(image, color, tolerance, softness, despill)
        elif operation == 'channel':
            channel = v.choice(body.get('channel', 'luminance'), '来源通道', ['luminance', 'red', 'green', 'blue', 'maximum'])
            invert = v.boolean(body.get('invert', False), '反转遮罩')
            combine = v.choice(body.get('combine', 'multiply'), '组合方式', ['multiply', 'replace'])
            image = channel_alpha(image, channel, invert, combine)
        elif operation == 'frame':
            mask = render_frame(store, project, body.get('mask_frame'))
            channel = v.choice(body.get('channel', 'alpha'), '遮罩通道', ['alpha', 'luminance'])
            mode = v.choice(body.get('combine', 'multiply'), '遮罩方式', ['multiply', 'replace', 'subtract', 'minimum', 'maximum'])
            invert = v.boolean(body.get('invert', False), '反转遮罩')
            image = apply_alpha_mask(image, mask, channel, invert, mode)
        else:
            radius = v.integer(body.get('radius', 1), '细修半径', 1, 12)
            threshold = v.integer(body.get('threshold', 128), '透明度阈值', 0, 255)
            image = refine_alpha(image, operation, radius, threshold)
        layer['asset'] = store.put_image(project_id, image)
    return store.get(project_id)


def alpha_report(image):
    array = np.asarray(image.convert('RGBA'))
    alpha = array[:, :, 3]
    counts = np.bincount(alpha.ravel(), minlength=256)
    transparent = alpha == 0
    hidden_color = transparent & np.any(array[:, :, :3] != 0, axis=2)
    return {
        'transparent_pixels': int(transparent.sum()),
        'opaque_pixels': int(np.count_nonzero(alpha == 255)),
        'partial_pixels': int(np.count_nonzero((alpha > 0) & (alpha < 255))),
        'hidden_rgb_pixels': int(hidden_color.sum()),
        'alpha_histogram': counts.tolist(),
        'alpha_mean': round(float(alpha.mean()), 6),
        'note': '隐藏RGB可能用于图集采样防色边；不要把隐藏RGB误判为可见内容',
    }
