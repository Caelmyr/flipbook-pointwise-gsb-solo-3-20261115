import math
import numpy as np
from PIL import Image
from . import validate as v
from .model import find_frame, new_frame, new_id
from .composite import render_frame

MAX_VOXELS = 1500000
CHUNK_ENTRIES = 2000000
BUFFER_SUBPIXELS = 4200000
BACK_SHADE = 0.85
SIDE_SHADES = (0.72, 0.55)
TOP_SHADE = 1.15
BOTTOM_SHADE = 0.5


def optional_color(body, name):
    value = body.get(name)
    if value in (None, ''):
        return None
    return np.array(v.rgba(value)[:3], dtype=np.float32) / 255


def extrusion_settings(body):
    return {
        'depth': v.integer(body.get('depth', 12), '挤出厚度', 1, 64),
        'threshold': v.integer(body.get('threshold', 0), '轮廓Alpha阈值', 0, 254),
        'elevation': v.real(body.get('elevation', 0), '俯视仰角', 0, 45),
        'side_rgb': optional_color(body, 'side_color'),
        'back_rgb': optional_color(body, 'back_color'),
        'size': v.choice(body.get('size', 'fit'), '输出尺寸', ['fit', 'canvas']),
        'padding': v.integer(body.get('padding', 2), '边缘留白', 0, 32),
    }


def animation_angles(body):
    start = v.real(body.get('start', 0), '起始角度', -360, 360)
    end = v.real(body.get('end', 360), '结束角度', -360, 360)
    if end == start:
        raise v.Invalid('结束角度需要与起始角度不同')
    if abs(end - start) > 360:
        raise v.Invalid('转台角度范围不能超过360度')
    count = v.integer(body.get('count', 24), '转台帧数', 2, 240)
    loop = v.boolean(body.get('loop', True), '循环')
    step = (end - start) / (count if loop else count - 1)
    return [start + step * i for i in range(count)], loop


def snapped_trig(value):
    rounded = round(value)
    return float(rounded) if abs(value - rounded) < 1e-12 else value


def rotation_bounds(width, height, depth, angle, elevation):
    yaw = math.radians(angle)
    pitch = math.radians(elevation)
    cos_t, sin_t = snapped_trig(math.cos(yaw)), snapped_trig(math.sin(yaw))
    cos_p, sin_p = snapped_trig(math.cos(pitch)), snapped_trig(math.sin(pitch))
    corners = []
    for dx in (-width / 2, width / 2):
        for dy in (-height / 2, height / 2):
            for dz in (-depth / 2, depth / 2):
                x1 = dx * cos_t + dz * sin_t
                z1 = -dx * sin_t + dz * cos_t
                y2 = dy * cos_p - z1 * sin_p
                corners.append((x1, y2))
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return min(xs), min(ys), max(xs), max(ys)


def fit_layout(width, height, depth, angles, elevation, padding):
    boxes = [rotation_bounds(width, height, depth, angle, elevation) for angle in angles]
    min_x = min(box[0] for box in boxes)
    min_y = min(box[1] for box in boxes)
    max_x = max(box[2] for box in boxes)
    max_y = max(box[3] for box in boxes)
    out_w = math.ceil(max_x - min_x) + 2 * padding
    out_h = math.ceil(max_y - min_y) + 2 * padding
    return out_w, out_h, padding - min_x, padding - min_y


def canvas_layout(width, height):
    return width, height, width / 2, height / 2


def check_layout(layout):
    if layout[0] > 2048 or layout[1] > 2048:
        raise v.Invalid('转台输出画布超过2048像素，请缩小源画布或降低厚度')


def face_colors(source, mask, vx, vy, vz, depth, view, side_rgb, back_rgb):
    height, width = mask.shape
    vo_x, vo_y, vo_z = view
    count = len(vx)
    step_x = int(np.sign(vo_x))
    step_y = int(np.sign(vo_y))
    step_z = int(np.sign(vo_z))
    if step_x:
        nx = vx - step_x
        inside = (nx >= 0) & (nx < width)
        x_exposed = ~inside | ~mask[vy, np.clip(nx, 0, width - 1)]
    else:
        x_exposed = np.zeros(count, bool)
    if step_y:
        ny = vy - step_y
        inside = (ny >= 0) & (ny < height)
        y_exposed = ~inside | ~mask[np.clip(ny, 0, height - 1), vx]
    else:
        y_exposed = np.zeros(count, bool)
    if step_z:
        nz = vz - step_z
        z_exposed = (nz < 0) | (nz >= depth)
    else:
        z_exposed = np.zeros(count, bool)
    score_x = np.where(x_exposed & (vo_x != 0), abs(vo_x), -1.0)
    score_y = np.where(y_exposed & (vo_y != 0), abs(vo_y), -1.0)
    score_z = np.where(z_exposed & (vo_z != 0), abs(vo_z), -1.0)
    visible = (score_x >= 0) | (score_y >= 0) | (score_z >= 0)
    face = np.argmax(np.stack([score_x, score_y, score_z]), axis=0)
    base = source[vy, vx].astype(np.float32) / 255
    rgb = base[:, :3]
    z_color = rgb
    if step_z < 0:
        z_color = back_rgb if back_rgb is not None else rgb * BACK_SHADE
        z_color = np.broadcast_to(z_color, rgb.shape) if z_color.ndim == 1 else z_color
    x_color = rgb
    if step_x:
        shade = SIDE_SHADES[0] if step_x < 0 else SIDE_SHADES[1]
        x_color = (side_rgb if side_rgb is not None else rgb) * shade
    y_color = rgb
    if step_y:
        y_color = rgb * (TOP_SHADE if step_y > 0 else BOTTOM_SHADE)
    color = np.where((face == 0)[:, None], x_color, np.where((face == 1)[:, None], y_color, z_color))
    alpha = np.where(visible, np.where(face == 2, base[:, 3], 1.0), 0.0)
    return np.clip(color, 0, 1), alpha[:, None]


def composite_chunk(buffer, rows, cols, depth, colors, blend):
    pixel = (rows * buffer.shape[1] + cols).astype(np.int64)
    if not len(pixel):
        return
    quantized = np.rint((depth - depth.min()) * 4096).astype(np.int64)
    if not blend:
        order = np.argsort(pixel * 33554432 + quantized, kind='stable')
        nearest = order[np.r_[True, pixel[order][1:] != pixel[order][:-1]]]
        buffer.reshape(-1, 4)[pixel[nearest]] = colors[nearest]
        return
    order = np.argsort(pixel * 33554432 + (quantized.max() - quantized), kind='stable')
    pixel = pixel[order]
    colors = colors[order]
    starts = np.r_[0, np.flatnonzero(np.diff(pixel)) + 1]
    groups = np.repeat(np.arange(len(starts)), np.diff(np.r_[starts, len(pixel)]))
    batch = np.arange(len(pixel)) - starts[groups]
    for level in range(batch.max() + 1):
        selected = batch == level
        positions = pixel[selected]
        over = colors[selected]
        alpha = over[:, 3:4]
        base = buffer.reshape(-1, 4)[positions]
        buffer.reshape(-1, 4)[positions] = np.concatenate([over[:, :3] + base[:, :3] * (1 - alpha), alpha + base[:, 3:4] * (1 - alpha)], axis=1)


def render_extrusion(source, settings, angle, layout):
    out_w, out_h, offset_x, offset_y = layout
    height, width = source.shape[:2]
    depth = settings['depth']
    mask = source[:, :, 3] > settings['threshold']
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return Image.fromarray(np.zeros((out_h, out_w, 4), dtype=np.uint8))
    if len(xs) * depth > MAX_VOXELS:
        raise v.Invalid('挤出体素超过150万，请缩小画布、裁去空白或降低厚度')
    supersample = 3
    while supersample > 1 and out_w * out_h * supersample * supersample > BUFFER_SUBPIXELS:
        supersample -= 1
    buffer = np.zeros((out_h * supersample, out_w * supersample, 4), dtype=np.float32)
    vx = np.repeat(xs, depth)
    vy = np.repeat(ys, depth)
    vz = np.tile(np.arange(depth), len(xs))
    yaw = math.radians(angle)
    pitch = math.radians(settings['elevation'])
    cos_t, sin_t = snapped_trig(math.cos(yaw)), snapped_trig(math.sin(yaw))
    cos_p, sin_p = snapped_trig(math.cos(pitch)), snapped_trig(math.sin(pitch))
    view = (-sin_t, cos_t * sin_p, cos_t * cos_p)
    rgb, alpha = face_colors(source, mask, vx, vy, vz, depth, view, settings['side_rgb'], settings['back_rgb'])
    colors = np.concatenate([rgb * alpha, alpha], axis=1)
    blend = bool(np.any(source[:, :, 3][mask] < 255))
    dx = vx - (width - 1) / 2
    dy = vy - (height - 1) / 2
    dz = vz - (depth - 1) / 2
    x1 = dx * cos_t + dz * sin_t
    z1 = -dx * sin_t + dz * cos_t
    screen_x = (x1 + offset_x) * supersample
    screen_y = ((dy * cos_p - z1 * sin_p) + offset_y) * supersample
    depth_value = dy * sin_p + z1 * cos_p
    order = np.argsort(-depth_value, kind='stable')
    foot_w = abs(cos_t) + abs(sin_t)
    foot_h = abs(cos_p) + abs(sin_p) * (abs(sin_t) + abs(cos_t))
    kernel_w = max(1, math.ceil(foot_w * supersample))
    kernel_h = max(1, math.ceil(foot_h * supersample))
    per_voxel = kernel_w * kernel_h
    rows_limit, cols_limit = buffer.shape[0], buffer.shape[1]
    for begin in range(0, len(vx), max(1, CHUNK_ENTRIES // per_voxel)):
        part = order[begin:begin + max(1, CHUNK_ENTRIES // per_voxel)]
        center_x = screen_x[part]
        center_y = screen_y[part]
        part_depth = depth_value[part]
        part_colors = colors[part]
        chunk_rows = []
        chunk_cols = []
        chunk_depth = []
        chunk_colors = []
        for ky in range(kernel_h):
            for kx in range(kernel_w):
                cols = np.rint(center_x - kernel_w / 2).astype(np.int64) + kx
                rows = np.rint(center_y - kernel_h / 2).astype(np.int64) + ky
                valid = (cols >= 0) & (cols < cols_limit) & (rows >= 0) & (rows < rows_limit)
                chunk_rows.append(rows[valid])
                chunk_cols.append(cols[valid])
                chunk_depth.append(part_depth[valid])
                chunk_colors.append(part_colors[valid])
        if not chunk_rows:
            continue
        composite_chunk(buffer, np.concatenate(chunk_rows), np.concatenate(chunk_cols), np.concatenate(chunk_depth), np.concatenate(chunk_colors), blend)
    blended = buffer.reshape(out_h, supersample, out_w, supersample, 4).mean(axis=(1, 3))
    alpha_out = blended[:, :, 3:4]
    rgb_out = np.divide(blended[:, :, :3], alpha_out, out=np.zeros_like(blended[:, :, :3]), where=alpha_out > 1e-6)
    result = np.concatenate([rgb_out, alpha_out], axis=2)
    return Image.fromarray(np.rint(np.clip(result, 0, 1) * 255).astype(np.uint8))


def source_pixels(store, project, body):
    frame = find_frame(project, body.get('frame_id'))
    image = render_frame(store, project, frame['id'])
    return frame, np.asarray(image.convert('RGBA'))


def preview_image(store, project, body):
    settings = extrusion_settings(body)
    angle = v.real(body.get('angle', 0), '预览角度', -360, 360)
    _, source = source_pixels(store, project, body)
    height, width = source.shape[:2]
    if settings['size'] == 'canvas':
        layout = canvas_layout(width, height)
    else:
        layout = fit_layout(width, height, settings['depth'], [angle], settings['elevation'], settings['padding'])
    check_layout(layout)
    return render_extrusion(source, settings, angle, layout)


def build_turntable(store, project_id, body):
    with store.lock:
        original = store.get(project_id)
        settings = extrusion_settings(body)
        angles, _ = animation_angles(body)
        duration = v.duration(body.get('duration', 80))
        frame, source = source_pixels(store, original, body)
        if not np.any(source[:, :, 3] > settings['threshold']):
            raise v.Invalid('当前帧没有可见像素，无法挤出立体轮廓')
        height, width = source.shape[:2]
        if settings['size'] == 'canvas':
            layout = canvas_layout(width, height)
        else:
            layout = fit_layout(width, height, settings['depth'], angles, settings['elevation'], settings['padding'])
        check_layout(layout)
        v.pixel_budget(layout[0], layout[1], len(angles))
        created = store.create(original['name'][:88] + ' · 立体转台', layout[0], layout[1])
        with store.edit(created['id'], 0, '生成立体转台动画') as project:
            project['frames'] = []
            project['palette'] = original['palette'][:]
            project['background'] = '#00000000'
            project['loop'] = 0
            for index, angle in enumerate(angles):
                image = render_extrusion(source, settings, angle, layout)
                asset = store.put_image(created['id'], image)
                project['frames'].append(new_frame(asset, f'转台 {index + 1}/{len(angles)} · {angle:.0f}°', duration))
            project['clips'] = [{
                'id': new_id(),
                'name': '转台循环',
                'frame_ids': [item['id'] for item in project['frames']],
                'direction': 'forward',
                'repeat': 0,
            }]
            project['notes'] = (
                f'来源项目{project_id}帧“{frame["name"]}”修订{original["revision"]}；'
                f'厚度{settings["depth"]}，角度{angles[0]:.0f}°–{angles[-1]:.0f}°，仰角{settings["elevation"]:.0f}°，'
                f'透明背景，原项目未改动。'
            )
        return store.get(created['id'])
