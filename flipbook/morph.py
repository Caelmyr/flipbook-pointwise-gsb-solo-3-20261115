"""Feature correspondence in-between transitions.

A plain crossfade mixes two drawings pixel by pixel, so both eyes and both
outlines stay visible at once. This module lets the artist mark corresponding
feature positions on the two key drawings, optionally pin a local reveal window
to one of them, and warps the drawings through a piecewise affine mesh.
Transparency follows the moving shapes instead of hiding a mismatch.
Contradictory marks (crossed correspondence, folded or degenerate mesh
triangles, impossible stretching) are reported with the feature names instead
of being silently averaged away.
"""
import hashlib
import json
import math
import numpy as np
from PIL import Image, ImageDraw
from . import validate as v
from .composite import render_frame
from .model import find_frame, new_frame, new_id
from .tween import ease


EASINGS = ['linear', 'ease_in', 'ease_out', 'ease_in_out', 'smoothstep']
MERGES = ['dither', 'blend']
PAIR_LIMIT = 24
REVEAL_LIMIT = 12
MINIMUM_PAIRS = 2
MINIMUM_SPACING = 1.0
MINIMUM_WINDOW = 2.0
MAXIMUM_FEATHER = 64.0
STRETCH_WARNING = 3.0
STRETCH_LIMIT = 6.0
SWIRL_WARNING = 150.0
TRANSITION_BUDGET = 50000000
BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], dtype=float)
DEFAULT_REVEAL = {'from': 'second', 'at': 0.5, 'feather': 0.25}


class CorrespondenceError(v.Invalid):
    """Contradictory correspondence, reported with per-feature detail."""

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = problems or []


def digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def frame_fingerprint(store, project, frame_id):
    """Fingerprint of the rendered original, so a later edit invalidates reuse."""
    frame = find_frame(project, frame_id)
    layers = [{key: layer.get(key) for key in ('asset', 'visible', 'opacity', 'blend', 'transform')}
              for layer in frame['layers']]
    raster = render_frame(store, project, frame_id)
    return digest({'frame': frame_id, 'layers': layers,
                   'pixel_sha256': hashlib.sha256(raster.tobytes()).hexdigest(),
                   'size': list(raster.size)})


def normalize_pairs(value, width, height):
    """Validate feature marks and return them in canvas pixel coordinates."""
    result = []
    pairs = v.array(value, '特征对应点', PAIR_LIMIT)
    if len(pairs) < MINIMUM_PAIRS:
        raise v.Invalid('至少需要两个特征对应点才能建立局部运动')
    reveals = 0
    for index, raw in enumerate(pairs):
        v.object_value(raw, '特征对应点')
        pair = {
            'label': v.text(raw.get('label', f'特征 {index + 1}'), '特征名称', 40, False),
            'first': _position(raw.get('first'), '特征甲侧', width, height),
            'second': _position(raw.get('second'), '特征乙侧', width, height),
        }
        if math.dist(pair['first'], pair['second']) < 1e-6:
            pair['stationary'] = True
        if raw.get('reveal') is not None:
            reveals += 1
            if reveals > REVEAL_LIMIT:
                raise v.Invalid(f'最多{REVEAL_LIMIT}个局部显露范围')
            pair['reveal'] = _reveal(raw['reveal'], width, height)
        result.append(pair)
    for index, pair in enumerate(result):
        for other in result[index + 1:]:
            if (math.dist(pair['first'], other['first']) < MINIMUM_SPACING
                    and math.dist(pair['second'], other['second']) < MINIMUM_SPACING):
                raise CorrespondenceError(
                    f'特征「{pair["label"]}」与「{other["label"]}」标记在同一位置',
                    [{'kind': 'duplicate', 'pairs': [pair['label'], other['label']],
                      'note': '两个对应点重合；请移动、分开或删除其中一个'}],
                )
    return result


def _position(raw, name, width, height):
    v.object_value(raw, name)
    if 'x' not in raw or 'y' not in raw:
        raise v.Invalid(name + '需要x与y坐标')
    x = v.real(raw['x'], name + 'X', -1, width)
    y = v.real(raw['y'], name + 'Y', -1, height)
    if not (0 <= x < width and 0 <= y < height):
        raise v.Invalid(name + '坐标超出画布')
    return [x, y]


def _reveal(raw, width, height):
    v.object_value(raw, '局部显露范围')
    owner = v.choice(raw.get('from', DEFAULT_REVEAL['from']), '显露来源帧', ['first', 'second'])
    x = v.real(raw.get('x', 0), '显露起点X', 0, width - MINIMUM_WINDOW)
    y = v.real(raw.get('y', 0), '显露起点Y', 0, height - MINIMUM_WINDOW)
    w = v.real(raw.get('width', width), '显露宽度', MINIMUM_WINDOW, width - x)
    h = v.real(raw.get('height', height), '显露高度', MINIMUM_WINDOW, height - y)
    at = v.real(raw.get('at', DEFAULT_REVEAL['at']), '显露切换时间', 0, 1)
    feather = v.real(raw.get('feather', DEFAULT_REVEAL['feather']), '显露羽化', 0, MAXIMUM_FEATHER)
    return {'from': owner, 'x': x, 'y': y, 'width': w, 'height': h, 'at': at, 'feather': feather}


def reveal_windows(pairs):
    return [pair['reveal'] for pair in pairs if pair.get('reveal')]


def validate_schedule(body, pair_count):
    count = v.integer(body.get('count', 6), '过渡帧数量', 1, 60)
    duration = v.duration(body.get('duration', 80))
    easing = v.choice(body.get('easing', 'smoothstep'), '过渡缓动', EASINGS)
    merge = v.choice(body.get('merge', 'dither'), '合成方式', MERGES)
    if pair_count > PAIR_LIMIT:
        raise v.Invalid(f'最多{PAIR_LIMIT}个特征对应点')
    return count, duration, easing, merge


def _delaunay(points):
    """Bowyer-Watson triangulation; points are unique (x, y) control anchors."""
    span = max(max(abs(x), abs(y)) for x, y in points) + 10
    vertices = list(points) + [(-span, -span), (span * 3, -span), (-span, span * 3)]
    triangles = [(len(points), len(points) + 1, len(points) + 2)]

    def circumcircle(triangle):
        (ax, ay), (bx, by), (cx, cy) = (vertices[i] for i in triangle)
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-12:
            return None
        ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
        return ux, uy, (ax - ux) ** 2 + (ay - uy) ** 2

    for index in range(len(points)):
        px, py = vertices[index]
        bad = []
        for triangle in triangles:
            circle = circumcircle(triangle)
            if circle is None or (px - circle[0]) ** 2 + (py - circle[1]) ** 2 <= circle[2] + 1e-9:
                bad.append(triangle)
        boundary = []
        for triangle in bad:
            for edge in ((triangle[0], triangle[1]), (triangle[1], triangle[2]), (triangle[2], triangle[0])):
                reverse = (edge[1], edge[0])
                if any(reverse == item for item in boundary):
                    boundary.remove(reverse)
                elif any(edge == item for item in boundary):
                    boundary.remove(edge)
                else:
                    boundary.append(edge)
        triangles = [triangle for triangle in triangles if triangle not in bad]
        for a, b in boundary:
            triangles.append((a, b, index))
    return [triangle for triangle in triangles
            if all(vertex < len(points) for vertex in triangle)]


def _unique(points, tolerance=1e-6):
    result, origin = [], []
    for point in points:
        for existing, index in zip(result, origin):
            if math.dist(point, existing) < tolerance:
                break
        else:
            origin.append(len(result))
            result.append(point)
    return result, origin


def build_mesh(pairs, width, height):
    """Control points, boundary anchors and triangle connectivity in both frames."""
    sources = [tuple(pair['first']) for pair in pairs]
    targets = [tuple(pair['second']) for pair in pairs]
    anchors = [(0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1),
               (width / 2, 0), (width / 2, height - 1), (0, height / 2), (width - 1, height / 2)]
    all_sources = sources + anchors
    all_targets = targets + anchors
    points, origins = _unique(all_sources, tolerance=1e-6)
    mapping = {}
    for index in range(len(sources)):
        found = next(i for i, point in enumerate(points) if math.dist(point, sources[index]) < 1e-6)
        mapping[index] = found
    if len(mapping) != len(set(mapping.values())):
        raise CorrespondenceError('不同特征落在了同一网格顶点，请把对应点分开',
                                  [{'kind': 'collapse', 'note': '两个特征标记在几乎同一位置，网格无法区分'}])
    triangles = _delaunay(points)
    if not triangles:
        raise CorrespondenceError('对应点共线，无法建立覆盖画布的运动网格',
                                  [{'kind': 'degenerate', 'note': '请再把特征点分散到不同方向'}])
    vertex_targets = []
    for point in points:
        for source, target in zip(all_sources, all_targets):
            if math.dist(point, source) < 1e-6:
                vertex_targets.append(target)
                break
    return {'source': [list(p) for p in points], 'target': [list(p) for p in vertex_targets],
            'triangles': triangles, 'pairs': mapping, 'labels': [pair['label'] for pair in pairs]}


def _area(a, b, c):
    """Twice the signed area; positive is counter-clockwise in pixel space."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def vertex_names(mesh):
    names = {}
    for index, name in zip(mesh['pairs'].values(), mesh['labels']):
        names[index] = name
    return names


def check_mesh(mesh, samples=41):
    """Detect folds, collapses and impossible stretch across the whole span.

    A triangle that reverses orientation at any sampled time means the marked
    correspondence twists the drawing into itself (a local flip). A triangle
    that collapses means two features cannot both keep their identity while
    the drawing moves continuously between the marks. Stretch is only judged
    between two marked features; the canvas corners are pinned anchors and are
    allowed to spread.
    """
    names = vertex_names(mesh)
    named = set(names)
    problems = []
    source, target = mesh['source'], mesh['target']
    seen = set()
    for triangle in mesh['triangles']:
        involved = sorted(names[i] for i in triangle if i in names)
        if len(involved) < 2:
            continue
        key = tuple(involved)
        if key in seen:
            continue
        seen.add(key)
        initial = _area(*[source[i] for i in triangle])
        if abs(initial) < 1e-9:
            problems.append({'kind': 'degenerate', 'pairs': involved, 'time': 0.0,
                             'note': '这一组特征在起始画面上已落在一条直线上，无法构成局部区域'})
            continue
        stretch = _mesh_stretch(triangle, source, target, named)
        if stretch > STRETCH_LIMIT:
            problems.append({'kind': 'stretch', 'pairs': involved, 'time': 1.0,
                             'note': f'特征间的距离被拉到原来的{stretch:.1f}倍（上限{STRETCH_LIMIT:g}倍）'})
            continue
        if stretch > STRETCH_WARNING:
            problems.append({'kind': 'stretch_warning', 'pairs': involved, 'time': 1.0,
                             'note': f'特征间距离会拉伸到原来的{stretch:.1f}倍，中间动作可能失真'})
        for step in range(samples):
            time = step / (samples - 1)
            sample = [(source[i][0] * (1 - time) + target[i][0] * time,
                       source[i][1] * (1 - time) + target[i][1] * time) for i in triangle]
            current = _area(*sample)
            if abs(current) < 1e-7:
                problems.append({'kind': 'collapse', 'pairs': involved, 'time': round(time, 3),
                                 'note': '中间时刻网格被压扁成一条线，对应关系在此处冲突'})
                break
            if current * initial < 0:
                problems.append({'kind': 'flip', 'pairs': involved, 'time': round(time, 3),
                                 'note': '局部网格翻转：对应点被拉过了头，中间动作会镜像扭结'})
                break
    return problems


def check_folds(mesh, samples=25, grid=48):
    """Detect a piecewise map that folds over itself.

    Swapping two features along the same line can leave every individual
    triangle positively oriented yet still tear the drawing: triangles covering
    the middle end up on top of each other. Sampling a coarse grid over the
    transition and counting how many triangles claim the same point exposes
    that, and the offending feature names are reported.
    """
    names = vertex_names(mesh)
    source, target = mesh['source'], mesh['target']
    coordinates = np.linspace(0.5, grid - 0.5, grid)
    ys, xs = np.meshgrid(coordinates, coordinates, indexing='ij')
    for step in range(samples):
        time = step / (samples - 1)
        claimed = np.zeros((grid, grid), dtype=np.int32)
        involved = []
        first_time = None
        for triangle in mesh['triangles']:
            points = [(source[i][0] * (1 - time) + target[i][0] * time,
                       source[i][1] * (1 - time) + target[i][1] * time) for i in triangle]
            if abs(_area(*points)) < 1e-9:
                continue
            bary = _barycentric(xs, ys, points)
            if bary is None:
                continue
            # Strictly interior samples only: adjacent triangles share their
            # borders, and shared borders are not a fold.
            inside = (bary[0] > 0.02) & (bary[1] > 0.02) & (bary[2] > 0.02)
            if not inside.any():
                continue
            if int(np.count_nonzero(inside & (claimed > 0))) >= 3:
                labels = [names[i] for i in triangle if i in names]
                if labels:
                    involved.append(labels)
                    if first_time is None:
                        first_time = round(time, 3)
            claimed += inside.astype(np.int32)
        if involved:
            labels = sorted({name for group in involved for name in group})
            return [{'kind': 'fold', 'pairs': labels, 'time': first_time,
                    'note': '中间动作的网格彼此重叠：对应点把局部画面拖过了相邻区域，会互相穿插'}]
    return []


def _mesh_stretch(triangle, source, target, named):
    ratios = [1.0]
    for index, vertex in enumerate(triangle):
        for other in triangle[index + 1:]:
            if vertex not in named or other not in named:
                continue
            before = math.dist(source[vertex], source[other])
            after = math.dist(target[vertex], target[other])
            if before > 1e-6 and after > 1e-6:
                ratios.append(max(after / before, before / after))
    return max(ratios)


def _lerp_vertices(mesh, time):
    """The shared intermediate geometry both drawings are warped into.

    A drawing is never dragged past its own features: at the start the first
    drawing is in place and at the end the second drawing is in place. Both are
    carried to the same intermediate positions in between, and the reveal
    decides which drawing is visible there, so one eye never appears next to
    the other eye.
    """
    source, target = mesh['source'], mesh['target']
    return [[sx + (tx - sx) * time, sy + (ty - sy) * time]
            for (sx, sy), (tx, ty) in zip(source, target)]


def _affine(source, destination):
    """Column-major matrix mapping destination pixel coordinates back to source."""
    matrix = np.array([[destination[0][0], destination[1][0], destination[2][0]],
                       [destination[0][1], destination[1][1], destination[2][1]],
                       [1.0, 1.0, 1.0]], dtype=float)
    points = np.array([[point[0] for point in source],
                       [point[1] for point in source],
                       [1.0, 1.0, 1.0]], dtype=float)
    try:
        return points @ np.linalg.inv(matrix)
    except np.linalg.LinAlgError:
        return None


def _barycentric(px, py, triangle):
    (ax, ay), (bx, by), (cx, cy) = triangle
    denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(denominator) < 1e-12:
        return None
    first = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denominator
    second = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denominator
    return first, second, 1 - first - second


def _warp_array(source_pixels, mesh, time, backward=False):
    """Piecewise-affine warp of one drawing into the shared intermediate mesh.

    Both drawings are warped into the *same* intermediate geometry, so at the
    middle of the transition the first drawing has moved onto the second
    drawing's features instead of the two sitting side by side. `backward`
    selects where the drawing's own pixels are sampled from: the original
    geometry for the first drawing, the target geometry for the second.
    """
    height, width = source_pixels.shape[:2]
    output = np.zeros_like(source_pixels)
    filled = np.zeros((height, width), dtype=bool)
    vertices = _lerp_vertices(mesh, time)
    base = mesh['target'] if backward else mesh['source']
    for triangle in mesh['triangles']:
        points = [base[index] for index in triangle]
        destination = [vertices[index] for index in triangle]
        if abs(_area(*destination)) < 1e-9:
            continue
        affine = _affine(points, destination)
        if affine is None:
            continue
        left = max(0, int(math.floor(min(p[0] for p in destination))) - 1)
        right = min(width, int(math.ceil(max(p[0] for p in destination))) + 1)
        top = max(0, int(math.floor(min(p[1] for p in destination))) - 1)
        bottom = min(height, int(math.ceil(max(p[1] for p in destination))) + 1)
        if left >= right or top >= bottom:
            continue
        ys, xs = np.mgrid[top:bottom, left:right]
        px = xs + 0.5
        py = ys + 0.5
        bary = _barycentric(px, py, destination)
        if bary is None:
            continue
        free = ~filled[top:bottom, left:right]
        inside = (bary[0] >= -1e-6) & (bary[1] >= -1e-6) & (bary[2] >= -1e-6) & free
        if not inside.any():
            continue
        sx = affine[0, 0] * px + affine[0, 1] * py + affine[0, 2]
        sy = affine[1, 0] * px + affine[1, 1] * py + affine[1, 2]
        ix = np.floor(sx).astype(np.int32)
        iy = np.floor(sy).astype(np.int32)
        sample = inside & (ix >= 0) & (iy >= 0) & (ix < width) & (iy < height)
        if not sample.any():
            continue
        block = output[top:bottom, left:right]
        block[sample] = source_pixels[iy[sample], ix[sample]]
        filled[top:bottom, left:right] |= sample
    return output


def preview_transition(store, project, first_id, second_id, pairs, schedule):
    """Build the intermediate images without writing anything to the project."""
    a = np.asarray(render_frame(store, project, first_id).convert('RGBA'), dtype=np.uint8)
    b = np.asarray(render_frame(store, project, second_id).convert('RGBA'), dtype=np.uint8)
    width, height = project['width'], project['height']
    mesh = build_mesh(pairs, width, height)
    hard, warnings = correspondence_problems(pairs, width, height)
    if hard:
        raise CorrespondenceError(describe_problems(hard), hard)
    count, duration, easing, merge = validate_schedule(schedule, len(pairs))
    step_ms = _step_durations(duration, count, schedule.get('total'))
    frames = []
    for index in range(1, count + 1):
        time = ease(index / (count + 1), easing)
        warped_a = _warp_array(a, mesh, time)
        warped_b = _warp_array(b, mesh, time, backward=True)
        weight = reveal_weight(pairs, time, count, width, height, schedule.get('reveal'))
        image = merge_arrays(warped_a, warped_b, weight, merge, index)
        frames.append({'index': index, 'time': round(index / (count + 1), 4), 'eased': round(time, 4),
                       'image': image, 'start': int(sum(step_ms[:index - 1]))})
    return {'frames': frames, 'duration': step_ms, 'count': count, 'easing': easing, 'merge': merge,
            'warnings': warnings}


def _step_durations(duration, count, total):
    if total is None:
        return [duration] * count
    total = v.integer(total, '过渡总时长毫秒', count * 20, count * 10000)
    ideal = total / count
    values = [int(ideal)] * count
    for index in range(total - sum(values)):
        values[index % count] += 1
    if any(not 20 <= value <= 10000 for value in values):
        raise v.Invalid('过渡总时长无法按每帧20–10000毫秒分配')
    return values


def describe_problems(problems):
    narratives = {
        'flip': '处局部网格翻转（对应点被拉过了头，中间动作会镜像扭结）',
        'collapse': '处网格被压扁成一条线',
        'degenerate': '处网格三角形退化',
        'stretch': '处特征间距拉伸超过上限',
        'crossed': '处两条对应线真正交叉',
        'fold': '处网格在中间动作互相穿叠',
        'order': '无法组成一组过渡',
        'duplicate': '处特征标记重合',
    }
    lines = []
    for problem in problems[:4]:
        label = '、'.join('「' + name + '」' for name in problem.get('pairs') or []) or '周边网格'
        if problem['kind'] == 'order':
            lines.append(f'{label}{problem.get("note", "")}')
            continue
        moment = '' if problem.get('time') is None else f'在时间{problem["time"]:.2f}'
        lines.append(f'{label}{moment}{narratives.get(problem["kind"], "")}')
    if len(problems) > 4:
        lines.append(f'另有{len(problems) - 4}处冲突')
    if any(problem['kind'] == 'order' for problem in problems):
        return '无法生成中间帧：' + '；'.join(lines) + '。'
    return '对应关系冲突：' + '；'.join(lines) + '。请调整对应点后再试。'


def _temporal_weight(time, switch, ramp):
    edge = max(min(ramp / 2, 0.3), 1e-4)
    lower = switch - edge
    upper = switch + edge
    if time <= lower:
        return 0.0
    if time >= upper:
        return 1.0
    return (time - lower) / (upper - lower)


def reveal_weight(pairs, time, count, width, height, override=None):
    """Per-pixel weight of the second drawing at this moment.

    Throughout the frame the first drawing is held until the switch time and
    then the second takes over. A per-pixel crossfade would show both copies at
    once, which is exactly the double-eye problem this tool exists to avoid.
    A reveal window pins its local area to one drawing until its own switch
    time, so an eye from the second drawing is not visible next to the first
    drawing's eye at the same moment.
    """
    windows = reveal_windows(pairs)
    step = 1 / (count + 1)
    ramp = max(min(step, 0.5), 1e-4)
    if override is None:
        global_switch, global_ramp = 0.5, ramp
    else:
        v.object_value(override, '显露时间设置')
        global_switch = v.real(override.get('at', 0.5), '全局显露时间', 0.05, 0.95)
        global_ramp = v.real(override.get('ramp', ramp), '显露过渡宽度', 1e-3, 2)
    ys, xs = np.mgrid[0:height, 0:width]
    px = xs + 0.5
    py = ys + 0.5
    base = _temporal_weight(time, global_switch, global_ramp)
    weight = np.full((height, width), base, dtype=float)
    for window in windows:
        owner = window['from']
        at = window['at']
        feather = window['feather']
        temporal = _temporal_weight(time, at, ramp) if owner == 'second' else 1 - _temporal_weight(time, at, ramp)
        if not feather:
            coverage = ((px >= window['x']) & (px < window['x'] + window['width'])
                        & (py >= window['y']) & (py < window['y'] + window['height'])).astype(float)
        else:
            half_w, half_h = window['width'] / 2, window['height'] / 2
            cx, cy = window['x'] + half_w, window['y'] + half_h
            # The fully-opaque core must be the marked rectangle itself, with
            # the feather only outside it; otherwise the rectangle shrinks by
            # the feather width and the window stops covering the local feature.
            distance = np.maximum(np.maximum(np.abs(px - cx) - half_w, 0),
                                  np.maximum(np.abs(py - cy) - half_h, 0)) / feather
            coverage = np.clip(1 - distance, 0, 1)
        weight = weight * (1 - coverage) + temporal * coverage
    return weight


def merge_arrays(left, right, weight, mode, dither_offset=0):
    """Combine the two warped drawings while keeping the silhouette opaque.

    Both drawings are already warped onto the same intermediate geometry, so
    overlapping them does not create a second outline. The silhouette is
    therefore taken as the union of the two warped shapes; it is never reduced
    to hide a mismatch. 'dither' picks one drawing per pixel with an ordered
    threshold (keeps flat pixel colours); 'blend' mixes colour but still keeps
    the union alpha, so no milky semi-transparent fringe appears.
    """
    weight = np.asarray(weight, dtype=float)
    if weight.ndim == 2:
        weight = weight[:, :, None]
    alpha_left = left[:, :, 3:4].astype(float) / 255
    alpha_right = right[:, :, 3:4].astype(float) / 255
    silhouette = np.maximum(alpha_left, alpha_right)
    if mode == 'dither':
        rows, columns = left.shape[0], left.shape[1]
        threshold = (BAYER4[np.arange(rows) % 4][:, np.arange(columns) % 4] + 0.5) / 16
        threshold = threshold[:, :, None]
        shift = (dither_offset % 16) / 16
        choice = weight > (threshold + shift) % 1
        pixels = np.where(choice, right, left)
        fallback = np.where(choice, left, right)
        # Where the chosen drawing has no coverage, use the other drawing.
        use_fallback = (pixels[:, :, 3:4] == 0) & (fallback[:, :, 3:4] > 0)
        pixels = np.where(use_fallback, fallback, pixels)
        color = pixels[:, :, :3].astype(float) / 255
        return _assemble(color, pixels[:, :, 3:4].astype(float) / 255)
    left_color = left[:, :, :3].astype(float) / 255
    right_color = right[:, :, :3].astype(float) / 255
    left_weight = (1 - weight) * alpha_left
    right_weight = weight * alpha_right
    total = left_weight + right_weight
    color = np.divide(left_color * left_weight + right_color * right_weight,
                      total, out=np.zeros_like(left_color), where=total > 1e-6)
    return _assemble(color, silhouette)


def _assemble(color, alpha):
    result = np.concatenate([color, alpha], axis=2)
    return np.rint(np.clip(result, 0, 1) * 255).astype(np.uint8)


def pair_geometry(pairs, width, height):
    """Human-readable correspondence table for the UI."""
    items = []
    for pair in pairs:
        a, b = pair['first'], pair['second']
        items.append({
            'label': pair['label'],
            'first': [round(a[0], 2), round(a[1], 2)],
            'second': [round(b[0], 2), round(b[1], 2)],
            'distance': round(math.dist(a, b), 2),
            'angle': round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])), 1),
            'reveal': pair.get('reveal'),
        })
    return items


def correspondence_problems(pairs, width, height):
    """Split problems into hard conflicts and stretch-only warnings.

    Hard conflicts mean the marks themselves cannot describe one continuous
    motion: crossed correspondence lines, a mesh that flips or collapses during
    the transition, triangles that degenerate, or a pull past the stretch limit.
    These are reported with the feature names and the moment they occur instead
    of being silently averaged into a blurred picture.
    """
    hard = []
    for index, pair in enumerate(pairs):
        for other in pairs[index + 1:]:
            if _proper_crossing(pair['first'], pair['second'], other['first'], other['second']):
                hard.append({'kind': 'crossed', 'pairs': [pair['label'], other['label']], 'time': None,
                             'note': '两条对应线真正交叉；中间动作会把这两个特征拧在一起'})
    warnings = []
    mesh = build_mesh(pairs, width, height)
    for problem in [*check_mesh(mesh), *check_folds(mesh)]:
        if problem['kind'] == 'stretch_warning':
            warnings.append(problem)
        else:
            hard.append(problem)
    return hard, warnings


def _proper_crossing(a1, a2, b1, b2):
    """True only when the two correspondence lines properly cross.

    Two marks whose paths merely meet at an endpoint (both drawings put the
    same feature back where it started) are not a conflict, and should not be
    reported as one.
    """
    if min(math.dist(a1, b1), math.dist(a1, b2), math.dist(a2, b2), math.dist(a2, b1)) < 0.5:
        return False

    def orientation(p, q, r):
        value = (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])
        return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)

    first = orientation(a1, a2, b1)
    second = orientation(a1, a2, b2)
    third = orientation(b1, b2, a1)
    fourth = orientation(b1, b2, a2)
    return first != second and third != fourth


def _schedule(body):
    return {
        'count': body.get('count', 6),
        'duration': body.get('duration', 80),
        'total': body.get('total'),
        'easing': body.get('easing', 'smoothstep'),
        'merge': body.get('merge', 'dither'),
        'reveal': body.get('reveal'),
    }


def schedule_values(body, pairs):
    schedule = _schedule(body)
    count, duration, easing, merge = validate_schedule(schedule, len(pairs))
    total = v.integer(body.get('total') or count * duration, '过渡总时长毫秒', count * 20, count * 10000)
    return schedule, count, duration, easing, merge, total


def _timeline_rows(project, first_id, second_id, generated, durations):
    """Order and per-frame timing for the planned or stored transition."""
    indexes = {frame['id']: index for index, frame in enumerate(project['frames'])}
    start = indexes[first_id] + 1
    # In-betweens from this same recipe may already sit between the keys; they
    # will be replaced, so the keys still count as adjacent.
    position = start
    while position < len(project['frames']) and project['frames'][position].get('morph_group'):
        position += 1
    adjacency = position == indexes.get(second_id)
    rows = [{'position': 'original', 'role': 'first', 'id': first_id,
             'name': project['frames'][indexes[first_id]]['name'],
             'duration': project['frames'][indexes[first_id]]['duration'], 'start': None}]
    elapsed = 0
    for index, (identifier, duration) in enumerate(zip(generated, durations)):
        rows.append({'position': f'{index + 1}/{len(generated)}', 'role': 'generated', 'id': identifier,
                     'name': f'过渡 {index + 1} / {len(generated)}', 'duration': duration, 'start': elapsed})
        elapsed += duration
    rows.append({'position': 'original', 'role': 'second', 'id': second_id,
                 'name': project['frames'][indexes[second_id]]['name'],
                 'duration': project['frames'][indexes[second_id]]['duration'], 'start': None})
    return {'insertion_index': start, 'adjacent_originals': adjacency, 'rows': rows,
            'timeline_note': None if adjacency else '第二张原画不是第一张原画的下一个帧，无法把它们当作一组过渡；请先调整帧序。'}


def _adjacent(project, first_id, second_id, group_id=None):
    """True when the second key drawing follows the first without other frames.

    In-betweens already generated from this very pair do not count: they are
    the frames that would be replaced, so adjusting marks and recomputing must
    still see the two key drawings as one pair.
    """
    frames = project['frames']
    indexes = {frame['id']: index for index, frame in enumerate(frames)}
    previous = next((item for item in project.get('morph_groups', [])
                     if item['first'] == first_id and item['second'] == second_id), None)
    if previous is not None:
        group_id = group_id or previous['id']
    position = indexes[first_id] + 1
    while position < len(frames) and group_id and frames[position].get('morph_group') == group_id:
        position += 1
    return position < len(frames) and frames[position]['id'] == second_id


def _generated_frames(project, group_id):
    return [frame for frame in project['frames'] if frame.get('morph_group') == group_id]


def _fingerprint_pair(store, project, first_id, second_id):
    return frame_fingerprint(store, project, first_id), frame_fingerprint(store, project, second_id)


class Morph:
    """Feature-correspondence transitions exposed as editable frames."""

    def __init__(self, store):
        self.store = store

    def _prepare(self, project_id, body):
        project = self.store.get(project_id)
        first_id = v.key(body.get('first'))
        second_id = v.key(body.get('second'))
        if first_id == second_id:
            raise v.Invalid('过渡需要两个不同的原画帧')
        first = find_frame(project, first_id)
        second = find_frame(project, second_id)
        pairs = normalize_pairs(body.get('pairs'), project['width'], project['height'])
        schedule, count, duration, easing, merge, total = schedule_values(body, pairs)
        hard, soft = correspondence_problems(pairs, project['width'], project['height'])
        plan = {
            'first': {'id': first_id, 'name': first['name']},
            'second': {'id': second_id, 'name': second['name']},
            'pairs': pair_geometry(pairs, project['width'], project['height']),
            'count': count,
            'duration': duration,
            'total': total,
            'easing': easing,
            'merge': merge,
            'problems': hard,
            'warnings': soft,
            'conflict': describe_problems(hard) if hard else None,
            'generated_names': [f'过渡 {index} / {count}' for index in range(1, count + 1)],
        }
        world = dict(schedule, count=count, duration=duration, easing=easing, merge=merge)
        if not _adjacent(project, first_id, second_id):
            plan['problems'].append({'kind': 'order', 'pairs': [first['name'], second['name']], 'time': None,
                                     'note': '两张原画之间夹着其他帧，生成帧无法形成连续过渡'})
            plan['conflict'] = describe_problems(plan['problems'])
        return project, first_id, second_id, pairs, world, plan

    def plan(self, project_id, body):
        project, first_id, second_id, pairs, world, plan = self._prepare(project_id, body)
        plan.update(_timeline_rows(project, first_id, second_id, [None] * plan['count'],
                                   [plan['duration']] * plan['count']))
        plan['fits_frame_limit'] = len(project['frames']) + plan['count'] <= 300
        return plan

    def preview(self, project_id, body):
        project, first_id, second_id, pairs, world, plan = self._prepare(project_id, body)
        if plan['problems']:
            raise CorrespondenceError(plan['conflict'], plan['problems'])
        result = preview_transition(self.store, project, first_id, second_id, pairs, world)
        return contact_sheet(result, project['width'], project['height'])

    def generate(self, project_id, revision, body):
        with self.store.edit(project_id, revision, '生成特征对应中间帧') as project:
            first_id = v.key(body.get('first'))
            second_id = v.key(body.get('second'))
            if first_id == second_id:
                raise v.Invalid('过渡需要两个不同的原画帧')
            first = find_frame(project, first_id)
            second = find_frame(project, second_id)
            pairs = normalize_pairs(body.get('pairs'), project['width'], project['height'])
            schedule, count, duration, easing, merge, total = schedule_values(body, pairs)
            hard, _ = correspondence_problems(pairs, project['width'], project['height'])
            if hard:
                raise CorrespondenceError(describe_problems(hard), hard)
            if len(project['frames']) + count > 300:
                raise v.Invalid('过渡后超过300帧上限')
            existing = next((item for item in project.get('morph_groups', [])
                             if item['first'] == first_id and item['second'] == second_id), None)
            if not _adjacent(project, first_id, second_id, existing['id'] if existing else None):
                raise v.Invalid('两张原画之间夹着其他帧，无法把它们当作一组过渡；请先调整帧序')
            if existing:
                raise v.Invalid('这一对原画已经有过渡组，请调整对应点后使用重算，避免重复插入第二套中间帧')
            values = dict(schedule, count=count, duration=duration, easing=easing, merge=merge)
            result = preview_transition(self.store, project, first_id, second_id, pairs, values)
            group_id = new_id()
            additions = self._insert(project, first, result, group_id)
            project.setdefault('morph_groups', []).append({
                'id': group_id,
                'first': first_id,
                'second': second_id,
                'first_fingerprint': frame_fingerprint(self.store, project, first_id),
                'second_fingerprint': frame_fingerprint(self.store, project, second_id),
                'width': project['width'],
                'height': project['height'],
                'pairs': pairs,
                'count': count,
                'duration': result['duration'][0],
                'total': sum(result['duration']),
                'easing': result['easing'],
                'merge': result['merge'],
                'reveal': schedule.get('reveal'),
                'frames': [frame['id'] for frame in additions],
            })
        return {'project': self.store.get(project_id), 'group': group_id,
                'order': self.order(project_id, group_id), 'plan': self.plan(project_id, body)}

    def _insert(self, project, first, result, group_id):
        project_id = project['id']
        additions = []
        for generated in result['frames']:
            asset = self.store.put_image(project_id, Image.fromarray(generated['image']))
            frame = new_frame(asset, f'过渡 {generated["index"]} / {result["count"]}',
                              result['duration'][generated['index'] - 1])
            frame['notes'] = f'特征对应中间帧 {generated["index"]}/{result["count"]}，按原画对应点插值生成'
            frame['morph_group'] = group_id
            additions.append(frame)
        index = project['frames'].index(first) + 1
        project['frames'][index:index] = additions
        return additions

    def _pair_body(self, project, group, body):
        """Fill the request from the stored recipe, so only the marks must be sent."""
        first_id = group['first']
        second_id = group['second']
        request = dict(body)
        request['first'] = first_id
        request['second'] = second_id
        request.setdefault('count', group['count'])
        request.setdefault('duration', group['duration'])
        if request.get('total') in (None, 0, ''):
            request['total'] = group.get('total')
        request.setdefault('easing', group['easing'])
        request.setdefault('merge', group['merge'])
        if request.get('reveal') is None:
            request['reveal'] = group.get('reveal')
        return request

    def recompute(self, project_id, revision, body):
        """Recompute the same original pair after correspondence points changed.

        Only the marks need to change: the original frames, counts and timing
        come from the stored recipe, so the regenerated frames replace the old
        ones in the same slot with the same durations and order.
        """
        group_id = v.key(body.get('group'))
        with self.store.edit(project_id, revision, '按调整后的对应点重算过渡') as project:
            group = next((item for item in project.get('morph_groups', []) if item['id'] == group_id), None)
            if group is None:
                raise v.Invalid('找不到这组过渡的来源记录，请重新选择原画')
            body = self._pair_body(project, group, body)
            first = find_frame(project, body['first'])
            second = find_frame(project, body['second'])
            if first['id'] == second['id']:
                raise v.Invalid('过渡的两个原画帧不能是同一帧')
            fingerprint_first, fingerprint_second = _fingerprint_pair(self.store, project, body['first'], body['second'])
            if fingerprint_first != group['first_fingerprint']:
                raise v.Invalid('起始原画已被修改，同一对原画已无法重算；请重新选择对应点并生成新的过渡')
            if fingerprint_second != group['second_fingerprint']:
                raise v.Invalid('结束原画已被修改，同一对原画已无法重算；请重新选择对应点并生成新的过渡')
            if group['width'] != project['width'] or group['height'] != project['height']:
                raise v.Invalid('画布尺寸已改变，请重新生成过渡')
            if not _adjacent(project, body['first'], body['second'], group_id):
                raise v.Invalid('两张原画之间已插入其他帧，无法按同一对原画重算；请先调整帧序')
            existing = _generated_frames(project, group_id)
            if not existing:
                raise v.Invalid('这组过渡的生成帧已被删除，请重新生成')
            pairs = normalize_pairs(body.get('pairs'), project['width'], project['height'])
            schedule, count, duration, easing, merge, total = schedule_values(body, pairs)
            hard, _ = correspondence_problems(pairs, project['width'], project['height'])
            if hard:
                raise CorrespondenceError(describe_problems(hard), hard)
            if len(project['frames']) - len(existing) + count > 300:
                raise v.Invalid('重算后超过300帧上限')
            values = dict(schedule, count=count, duration=duration, easing=easing, merge=merge)
            if count != group['count']:
                raise v.Invalid('重算时过渡帧数量必须与原记录一致；如需改变数量请新建过渡')
            result = preview_transition(self.store, project, group['first'], group['second'], pairs, values)
            # Update the existing generated frames in place: same frame ids, same
            # slot and same neighbours, only the pixels (and if requested, the
            # durations) are recomputed from the adjusted marks.
            for frame, generated in zip(existing, result['frames']):
                asset = self.store.put_image(project_id, Image.fromarray(generated['image']))
                frame['layers'][0]['asset'] = asset
                frame['layers'][0]['transform'] = v.transform({})
                frame['layers'][0].pop('cel_id', None)
                frame['duration'] = result['duration'][generated['index'] - 1]
                frame['notes'] = f'特征对应中间帧 {generated["index"]}/{result["count"]}，对应点调整后重算'
            group.update({
                'pairs': pairs,
                'count': count,
                'duration': result['duration'][0],
                'total': sum(result['duration']),
                'easing': result['easing'],
                'merge': result['merge'],
                'reveal': schedule.get('reveal'),
                'frames': [frame['id'] for frame in existing],
            })
        return {'project': self.store.get(project_id), 'group': group_id,
                'order': self.order(project_id, group_id), 'plan': self.plan(project_id, body)}

    def order(self, project_id, group_id):
        project = self.store.get(project_id)
        group = next((item for item in project.get('morph_groups', []) if item['id'] == group_id), None)
        if group is None:
            raise v.Invalid('过渡组不存在')
        indexes = {frame['id']: index for index, frame in enumerate(project['frames'])}
        missing = [identifier for identifier in [group['first'], group['second'], *group['frames']]
                   if identifier not in indexes]
        if missing:
            raise v.Invalid('过渡组的原画或生成帧已被删除，请重新生成')
        rows = [{'position': '原画', 'role': 'first', 'id': group['first'],
                 'name': project['frames'][indexes[group['first']]]['name'],
                 'duration': project['frames'][indexes[group['first']]]['duration'], 'start': None}]
        elapsed = 0
        for position, identifier in enumerate(group['frames']):
            frame = project['frames'][indexes[identifier]]
            rows.append({'position': f'{position + 1}/{group["count"]}', 'role': 'generated', 'id': identifier,
                         'name': frame['name'], 'duration': frame['duration'], 'start': elapsed})
            elapsed += frame['duration']
        rows.append({'position': '原画', 'role': 'second', 'id': group['second'],
                     'name': project['frames'][indexes[group['second']]]['name'],
                     'duration': project['frames'][indexes[group['second']]]['duration'], 'start': None})
        order = [indexes[group['first']], *[indexes[identifier] for identifier in group['frames']],
                 indexes[group['second']]]
        contiguous = order == list(range(order[0], order[0] + len(order)))
        if not contiguous:
            return {'group': group_id, 'frames': group['frames'], 'duration': group['total'],
                    'contiguous': False, 'first': group['first'], 'second': group['second'], 'rows': rows,
                    'note': '生成帧之间已插入其他帧；时序表按当前时间轴排列如下，重新计算会移除本组旧帧后插入原位置。',
                    'timeline_indexes': order}
        return {
            'group': group_id,
            'first': {'id': group['first'], 'name': project['frames'][indexes[group['first']]]['name']},
            'second': {'id': group['second'], 'name': project['frames'][indexes[group['second']]]['name']},
            'frames': group['frames'],
            'count': group['count'],
            'duration': group['total'],
            'contiguous': True,
            'timeline_indexes': order,
            'rows': rows,
            'note': None,
        }

    def groups(self, project_id):
        project = self.store.get(project_id)
        result = []
        for item in project.get('morph_groups', []):
            try:
                result.append(self.order(project_id, item['id']))
            except v.Invalid as error:
                result.append({'group': item['id'], 'error': str(error)})
        return result

    def group_details(self, project_id, group_id):
        project = self.store.get(project_id)
        group = next((item for item in project.get('morph_groups', []) if item['id'] == group_id), None)
        if group is None:
            raise v.Invalid('过渡组不存在')
        return {'group': group_id, 'first': group['first'], 'second': group['second'],
                'pairs': pair_geometry(group['pairs'], project['width'], project['height']),
                'count': group['count'], 'duration': group['total'], 'easing': group['easing'],
                'merge': group['merge'], 'order': self.order(project_id, group_id)}


def contact_sheet(result, width, height, columns=None, scale=2):
    """Every generated frame side by side, with its index and duration."""
    frames = result['frames']
    total = len(frames)
    columns = columns or min(6, total)
    rows = math.ceil(total / columns)
    gap = 6
    label = 14
    sheet = Image.new('RGBA', (columns * (width * scale + gap) + gap,
                               rows * (height * scale + gap + label) + gap), '#1b2a37')
    for index, generated in enumerate(frames):
        column = index % columns
        row = index // columns
        left = gap + column * (width * scale + gap)
        top = gap + row * (height * scale + gap + label)
        image = Image.fromarray(generated['image']).resize((width * scale, height * scale), Image.Resampling.NEAREST)
        sheet.alpha_composite(image, (left, top))
        ImageDraw.Draw(sheet).text((left + 2, top + height * scale + 1),
                                   f'{generated["index"]}/{total} · {result["duration"][index]}ms',
                                   fill=(200, 225, 235))
    return sheet


def validate_entries(groups, project):
    """Keep stored recipes consistent with the frames that still exist."""
    known = {frame['id'] for frame in project['frames']}
    seen = set()
    for group in v.array(groups, '特征过渡组', 100):
        identifier = v.key(group.get('id'))
        if identifier in seen:
            raise v.Invalid('特征过渡组编号重复')
        seen.add(identifier)
        v.key(group.get('first'))
        v.key(group.get('second'))
        if group['first'] == group['second']:
            raise v.Invalid('特征过渡组的两个原画帧不能相同')
        for field in ('first_fingerprint', 'second_fingerprint'):
            if not isinstance(group.get(field), str) or len(group[field]) != 64:
                raise v.Invalid('特征过渡组缺少原画指纹')
        v.integer(group.get('count'), '特征过渡帧数', 1, 60)
        v.integer(group.get('width'), '特征过渡画布宽', 8, 2048)
        v.integer(group.get('height'), '特征过渡画布高', 8, 2048)
        frames = v.array(group.get('frames'), '特征过渡生成帧', 60)
        if len(frames) != group['count']:
            raise v.Invalid('特征过渡组的生成帧数量与记录不一致')
        if len(set(frames)) != len(frames):
            raise v.Invalid('特征过渡组的生成帧重复')
        if any(identifier not in known for identifier in frames):
            raise v.Invalid('特征过渡组引用了已删除的帧')
        if group['first'] not in known or group['second'] not in known:
            raise v.Invalid('特征过渡组的原画帧已被删除')
        v.choice(group.get('easing'), '特征过渡缓动', ['linear', 'ease_in', 'ease_out', 'ease_in_out', 'smoothstep'])
        v.choice(group.get('merge'), '特征过渡合成', ['dither', 'blend'])
        for pair in v.array(group.get('pairs'), '特征过渡对应点', PAIR_LIMIT):
            v.object_value(pair, '特征对应点')
            v.text(pair.get('label', ''), '特征名称', 40, False)
            for side in ('first', 'second'):
                point = v.array(pair.get(side), '特征坐标', 2)
                v.real(point[0], '特征坐标X', -1, project['width'])
                v.real(point[1], '特征坐标Y', -1, project['height'])
            reveal = pair.get('reveal')
            if reveal is not None:
                v.object_value(reveal, '局部显露范围')
                v.choice(reveal.get('from'), '显露来源帧', ['first', 'second'])
                v.real(reveal.get('at'), '显露切换时间', 0, 1)
                v.real(reveal.get('feather'), '显露羽化', 0, MAXIMUM_FEATHER)
    return groups
