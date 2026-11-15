"""Hierarchical bone rig, constrained IK and per-frame key poses.

Layers keep their own editable pixels.  A rig stores one bind transform per
bound layer (captured from the layer's existing position, rotation, scale and
mirror) plus a bone hierarchy with per-joint limits.  Posing only writes
ordinary layer transform dictionaries, so the normal compositor, the live
canvas preview and every export job read exactly the same joint and pixel
coordinates.
"""
import math
from copy import deepcopy
import numpy as np
from . import validate as v
from .layer_tree import local_matrix
from .model import find_frame, new_id
from .layer_nodes import walk_layers


MAX_BONES = 64
MAX_BINDINGS = 64
MAX_TARGETS = 16
MAX_DEPTH = 8
DEFAULT_LIMIT = 180.0


def normalize_angle(value):
    result = (float(value) + 180.0) % 360.0 - 180.0
    if result <= -180.0 + 1e-12:
        result = 180.0
    return result


def rotation_matrix(degrees):
    radians = math.radians(degrees)
    co, si = math.cos(radians), math.sin(radians)
    return np.array([[co, -si], [si, co]])


def rotation3(degrees):
    matrix = np.eye(3)
    matrix[:2, :2] = rotation_matrix(degrees)
    return matrix


def joint_matrix(origin, angle):
    matrix = rotation3(angle)
    matrix[:2, 2] = [float(origin[0]), float(origin[1])]
    return matrix


def centre_matrix(size):
    return np.array([[1., 0., size[0] / 2.], [0., 1., size[1] / 2.], [0., 0., 1.]])


def decompose_candidates(matrix):
    linear = np.asarray(matrix, dtype=float)[:2, :2]
    candidates = []
    for flip_x in (False, True):
        for flip_y in (False, True):
            dx = -1. if flip_x else 1.
            dy = -1. if flip_y else 1.
            column0 = linear[:, 0]
            sx = float(np.linalg.norm(column0))
            sy = float(np.linalg.norm(linear[:, 1]))
            if sx < 1e-8 or sy < 1e-8:
                continue
            angle = math.degrees(math.atan2(column0[1] * dx, column0[0] * dx))
            predicted = rotation_matrix(angle) @ np.diag([dx * sx, dy * sy])
            error = float(np.max(np.abs(predicted - linear)))
            for turn in (-360.0, 0.0, 360.0):
                candidates.append({
                    'error': error,
                    'angle': angle + turn,
                    'scale_x': sx,
                    'scale_y': sy,
                    'flip_x': flip_x,
                    'flip_y': flip_y,
                })
    return [item for item in candidates if item['error'] <= 0.002]


def transform_from_matrix(matrix, size, reference=None):
    """Decompose a pose matrix back into a layer transform.

    Bone posing only rotates and translates bound layers, so the matrix is
    always representable as position + rotation + axis scale + axis mirror.
    The recovered values reproduce the same matrix through ``local_matrix``.
    When a bind transform is supplied, an equivalent representation with the
    same mirror flags and the nearest rotation is chosen, so a pose that only
    moves a bone keeps the layer's original rotation and mirror values.
    """
    candidates = decompose_candidates(matrix)
    if not candidates:
        raise v.Invalid('姿态矩阵退化或包含剪切，无法恢复图层变换')
    best_error = min(item['error'] for item in candidates)
    candidates = [item for item in candidates if item['error'] <= best_error + 1e-6]
    variants = []
    for item in candidates:
        for turn in (-360.0, 0.0, 360.0):
            angle = item['angle'] + turn
            if -360.0 <= angle <= 360.0:
                variants.append(dict(item, angle=angle))
    if reference is not None:
        def score(item):
            return (
                int(item['flip_x'] != reference['flip_x']) + int(item['flip_y'] != reference['flip_y']),
                abs(item['angle'] - reference['rotation']),
                abs(item['angle']),
            )
    else:
        def score(item):
            return (abs(item['angle']), int(item['flip_x']), int(item['flip_y']))
    chosen = min(variants, key=score)
    angle, sx, sy = chosen['angle'], chosen['scale_x'], chosen['scale_y']
    scale = np.diag([(-sx if chosen['flip_x'] else sx), (-sy if chosen['flip_y'] else sy), 1.])
    rest = centre_matrix(size) @ rotation3(angle) @ scale @ np.linalg.inv(centre_matrix(size))
    translation = np.asarray(matrix, dtype=float)[:2, 2] - rest[:2, 2]
    transform = {
        'x': int(round(float(translation[0]))),
        'y': int(round(float(translation[1]))),
        'scale_x': round(sx, 6),
        'scale_y': round(sy, 6),
        'rotation': round(angle, 6),
        'flip_x': bool(chosen['flip_x']),
        'flip_y': bool(chosen['flip_y']),
    }
    return v.transform(transform)


def binding_rest(binding, size):
    return local_matrix(binding['bind'], size)


def binding_offset(binding, bone, size):
    """Binding transform expressed in the bone's own rotated frame."""
    return np.linalg.inv(joint_matrix(bone['origin'], bone['angle'])) @ binding_rest(binding, size)


def ancestry(bones, bone_id):
    path = []
    current = bones.get(bone_id)
    while current is not None:
        path.append(current['id'])
        parent = current.get('parent')
        current = bones.get(parent) if parent else None
        if len(path) > MAX_BONES:
            raise v.Invalid('骨骼父子关系存在环')
    return path[::-1]


def default_chain(bones, bone_id, target=None, offset=(0.0, 0.0), root=None):
    """Smallest usable chain: the target joint plus ancestors as needed.

    The rig root is excluded so that a hand/foot target never rotates the
    whole body; body movement is expressed through the pose root translation.
    Ancestors are added only while the target is out of reach for the bones
    already in the chain, which keeps a leg from hijacking the torso when the
    leg alone can reach a pinned foot position.
    """
    path = ancestry(bones, bone_id)
    if root:
        path = [item for item in path if item != root]
    chain = [bone_id]
    if target is not None:
        target = np.asarray(target, dtype=float)
        frames = world_frames(bones, {}, offset)
        reach = bones[bone_id]['length']
        origin = frames[bone_id]['joint']
        for ancestor in reversed(path[:-1]):
            if np.linalg.norm(target - origin) <= reach + 1e-6:
                break
            chain.insert(0, ancestor)
            reach += bones[ancestor]['length']
        return chain
    return path[-3:]


def limits(bone):
    return float(bone['limits']['min']), float(bone['limits']['max'])


def clamp_delta(bone, value):
    low, high = limits(bone)
    return max(low, min(high, float(value)))


def world_frames(bones, deltas, offset=(0.0, 0.0)):
    """Forward kinematics: joint, tip, orientation and accumulated angle."""
    cache = {}

    def visit(bone_id):
        if bone_id in cache:
            return cache[bone_id]
        bone = bones[bone_id]
        parent_id = bone.get('parent')
        if parent_id:
            parent = bones[parent_id]
            parent_frame = visit(parent_id)
            rest = np.array([bone['origin'][0] - parent['origin'][0],
                             bone['origin'][1] - parent['origin'][1]], dtype=float)
            joint = parent_frame['joint'] + rotation_matrix(parent_frame['accumulated']) @ rest
            inherited = parent_frame['accumulated']
        else:
            joint = np.array([bone['origin'][0] + offset[0], bone['origin'][1] + offset[1]], dtype=float)
            inherited = 0.0
        accumulated = inherited + deltas.get(bone_id, 0.0)
        orientation = bone['angle'] + accumulated
        direction = np.array([math.cos(math.radians(orientation)), math.sin(math.radians(orientation))])
        cache[bone_id] = {
            'joint': joint,
            'tip': joint + direction * bone['length'],
            'angle': orientation,
            'accumulated': accumulated,
            'direction': direction,
        }
        return cache[bone_id]

    for bone_id in bones:
        visit(bone_id)
    return cache


def signed_angle(first, second):
    if np.linalg.norm(first) < 1e-9 or np.linalg.norm(second) < 1e-9:
        return 0.0
    cross = first[0] * second[1] - first[1] * second[0]
    dot = float(np.asarray(first) @ np.asarray(second))
    return math.degrees(math.atan2(cross, dot))


def solve_chain(bones, deltas, offset, chain, target, iterations=40, tolerance=0.01):
    """Cyclic coordinate descent with joint limits and rigid bone lengths.

    Joint rotations are clamped to their configured range and bone lengths are
    never changed, so an out-of-reach target leaves a measurable residual
    instead of stretching a limb.  Segments nearer the target than a bone's
    own length pull the whole chain end (the usual CCD rule), which also moves
    a fully stretched chain; a deterministic nudge is used when iterations
    stop improving at a local minimum.  The best pose found is kept.
    """
    target = np.asarray(target, dtype=float)
    if not chain:
        raise v.Invalid('IK 关节链不能为空')
    for bone_id in chain:
        if bone_id not in bones:
            raise v.Invalid('IK 关节链包含不存在的骨骼')
    tip_id = chain[-1]
    counter = 0
    stalls = 0
    bend = 1.0
    best_error = None
    best = None
    for counter in range(1, iterations + 1):
        frames = world_frames(bones, deltas, offset)
        error = float(np.linalg.norm(frames[tip_id]['tip'] - target))
        if best_error is None or error < best_error - 1e-12:
            best_error, best = error, dict(deltas)
        if error <= tolerance:
            break
        for bone_id in reversed(chain):
            joint = world_frames(bones, deltas, offset)[bone_id]['joint']
            current = world_frames(bones, deltas, offset)[tip_id]['tip'] - joint
            desired = target - joint
            if np.linalg.norm(desired) < 1e-6:
                continue
            if np.linalg.norm(current) < 1e-6:
                # The chain has folded onto its own joint: nudge it off the
                # singularity instead of leaving a zero rotation step.
                low, high = limits(bones[bone_id])
                step = -8.0 if deltas.get(bone_id, 0.0) >= 0 else 8.0
                nudge = deltas.get(bone_id, 0.0) + step
                deltas[bone_id] = max(low, min(high, nudge))
                continue
            deltas[bone_id] = clamp_delta(bones[bone_id], deltas.get(bone_id, 0.0) + signed_angle(current, desired))
        improved = float(np.linalg.norm(world_frames(bones, deltas, offset)[tip_id]['tip'] - target))
        if improved >= error - 1e-9:
            stalls += 1
            if stalls > 8:
                break
            bend = -bend
            moved = False
            for bone_id in chain[::-1]:
                if bone_id == tip_id:
                    continue
                low, high = limits(bones[bone_id])
                candidate = deltas.get(bone_id, 0.0) + bend * 4.0
                if low <= candidate <= high:
                    deltas[bone_id] = candidate
                    moved = True
                    break
            if not moved:
                break
        else:
            stalls = 0
    if best is not None:
        stored = {bone_id: best.get(bone_id, deltas.get(bone_id, 0.0)) for bone_id in chain}
        deltas.clear()
        deltas.update(stored)
    frames = world_frames(bones, deltas, offset)
    reached = frames[tip_id]['tip']
    return {
        'reached': [round(float(reached[0]), 4), round(float(reached[1]), 4)],
        'residual': round(float(np.linalg.norm(reached - target)), 4),
        'iterations': counter,
    }


def residual_set(bones, deltas, offset, chains, targets):
    frames = world_frames(bones, deltas, offset)
    residuals = [np.linalg.norm(frames[chain[-1]]['tip'] - np.asarray(target, dtype=float))
                 for chain, target in zip(chains, targets)]
    return residuals, frames


def refine_targets(bones, deltas, offset, parsed_targets, iterations=160, tolerance=0.02):
    """Jointly refine several IK targets with a projected gradient step.

    Solving one chain at a time lets a shared parent joint satisfy the newest
    target at the cost of an earlier one; evaluating all residuals together
    keeps two pinned feet and a moved body consistent.  Joint rotations stay
    clamped and bone lengths never change, so an unreachable target keeps a
    positive residual.
    """
    targets = [np.asarray(item['target'], dtype=float) for item in parsed_targets]
    chains = [item['chain'] for item in parsed_targets]
    if not targets:
        return
    variables = []
    for chain in chains:
        for bone_id in chain:
            if bone_id not in variables:
                variables.append(bone_id)
    def measure(values):
        residuals, frames = residual_set(bones, values, offset, chains, targets)
        return float(sum(item * item for item in residuals)), float(max(residuals)), frames
    total, worst, frames = measure(deltas)
    for _ in range(iterations):
        if worst <= tolerance:
            break
        gradient = {bone_id: 0.0 for bone_id in variables}
        for chain, target in zip(chains, targets):
            tip = frames[chain[-1]]['tip']
            error = tip - target
            for bone_id in chain:
                joint = frames[bone_id]['joint']
                arm = tip - joint
                gradient[bone_id] += 2.0 * float(error[0] * (-arm[1]) + error[1] * arm[0])
        largest = max(abs(value) for value in gradient.values()) if gradient else 0.0
        if largest < 1e-12:
            break
        direction = {bone_id: gradient[bone_id] / largest for bone_id in variables}
        accepted = False
        step = 24.0
        for _ in range(14):
            candidate = {bone_id: clamp_delta(bones[bone_id], deltas.get(bone_id, 0.0) - step * direction[bone_id])
                         for bone_id in variables}
            candidate_total, candidate_worst, candidate_frames = measure(candidate)
            if candidate_total < total - 1e-9 or candidate_worst < worst - 1e-9:
                deltas.update(candidate)
                total, worst, frames = candidate_total, candidate_worst, candidate_frames
                accepted = True
                break
            step *= 0.5
        if not accepted:
            break


def read_origin(value, project, fallback):
    origin = v.array(value, '关节位置', 2) if value is not None else fallback
    if len(origin) != 2:
        raise v.Invalid('关节位置需要x和y')
    return [v.real(origin[0], '关节X', -4096, 4096), v.real(origin[1], '关节Y', -4096, 4096)]


def parse_bones(body, project):
    raw = v.array(body.get('bones'), '骨骼', MAX_BONES)
    if not raw:
        raise v.Invalid('至少建立一根骨骼')
    bones = []
    identifiers = set()
    for item in raw:
        item = v.object_value(item, '骨骼')
        identifier = v.key(item.get('id')) if item.get('id') else new_id()
        if identifier in identifiers:
            raise v.Invalid('骨骼编号重复')
        identifiers.add(identifier)
        ranged = item.get('limits') if isinstance(item.get('limits'), dict) else {}
        low = v.real(item.get('limit_min', ranged.get('min', -DEFAULT_LIMIT)), '关节最小角度', -360, 360)
        high = v.real(item.get('limit_max', ranged.get('max', DEFAULT_LIMIT)), '关节最大角度', -360, 360)
        if low > high:
            raise v.Invalid('关节最小角度不能大于最大角度')
        bone = {
            'id': identifier,
            'name': v.text(item.get('name'), '骨骼名称', 60, False),
            'parent': item.get('parent') or None,
            'origin': read_origin(item.get('origin'), project, [project['width'] / 2, project['height'] / 2]),
            'angle': v.real(item.get('angle', -90), '骨骼角度', -360, 360),
            'length': v.real(item.get('length', 8), '骨骼长度', 0.5, 4096),
            'limits': {'min': low, 'max': high},
            'target': v.boolean(item.get('target', False), 'IK 目标骨骼'),
        }
        if bone['parent'] is not None:
            v.key(bone['parent'])
        bones.append(bone)
    lookup = {bone['id']: bone for bone in bones}
    for bone in bones:
        if bone['parent'] and bone['parent'] not in lookup:
            raise v.Invalid('父骨骼不存在')
        seen = set()
        depth = 0
        current = bone
        while current and current.get('parent'):
            if current['id'] in seen:
                raise v.Invalid('骨骼父子关系存在环')
            seen.add(current['id'])
            current = lookup[current['parent']]
            depth += 1
            if depth > MAX_DEPTH:
                raise v.Invalid('骨骼层级不能超过8层')
    roots = [bone['id'] for bone in bones if not bone['parent']]
    root = body.get('root') or roots[0]
    v.key(root)
    if root not in lookup:
        raise v.Invalid('根骨骼不存在')
    if root not in roots:
        raise v.Invalid('根骨骼必须位于骨骼层级顶部')
    return bones, root


def parse_bindings(body, project, frame_id):
    raw = v.array(body.get('bindings', []), '图层绑定', MAX_BINDINGS)
    if not raw:
        return []
    bones = {bone['id']: bone for bone in body['bones']}
    frame = find_frame(project, frame_id) if frame_id else None
    lookup = {}
    if frame is not None:
        for layer in walk_layers(frame['layers']):
            lookup.setdefault(layer['name'], []).append(layer)
    bindings = []
    names = set()
    for item in raw:
        item = v.object_value(item, '图层绑定')
        bone_id = v.key(item.get('bone'))
        if bone_id not in bones:
            raise v.Invalid('绑定引用的骨骼不存在')
        name = v.text(item.get('layer'), '绑定图层名称', 100, False)
        if name in names:
            raise v.Invalid('同一图层只能绑定到一根骨骼')
        names.add(name)
        if item.get('bind') is not None:
            captured = v.transform(item['bind'])
        else:
            matches = lookup.get(name, [])
            if not matches:
                raise v.Invalid('绑定图层不存在或名称不唯一：' + name)
            if len(matches) > 1:
                raise v.Invalid('图层名称不唯一，无法绑定：' + name)
            captured = v.transform(matches[0]['transform'])
        bindings.append({'bone': bone_id, 'layer': name, 'bind': captured})
    return bindings


def validate_rig(rig, project):
    if rig is None:
        return None
    rig = v.object_value(rig, '骨骼绑定')
    size = v.array(rig.get('size', [project['width'], project['height']]), '骨骼绑定画布', 2)
    if [int(size[0]), int(size[1])] != [project['width'], project['height']]:
        raise v.Invalid('骨骼绑定画布与项目尺寸不一致')
    bones, root = parse_bones(rig, project)
    if rig.get('root') != root and rig.get('root') not in {bone['id'] for bone in bones}:
        raise v.Invalid('根骨骼不存在')
    bindings = []
    names = set()
    lookup = {bone['id'] for bone in bones}
    for item in v.array(rig.get('bindings', []), '图层绑定', MAX_BINDINGS):
        item = v.object_value(item, '图层绑定')
        if v.key(item.get('bone')) not in lookup:
            raise v.Invalid('绑定引用的骨骼不存在')
        name = v.text(item.get('layer'), '绑定图层名称', 100, False)
        if name in names:
            raise v.Invalid('同一图层只能绑定到一根骨骼')
        names.add(name)
        bindings.append({'bone': item['bone'], 'layer': name, 'bind': v.transform(item.get('bind'))})
    if rig.get('created_frame') is not None:
        v.key(rig['created_frame'])
    return {
        'bones': bones,
        'bindings': bindings,
        'root': root,
        'created_frame': rig.get('created_frame'),
        'size': [project['width'], project['height']],
        'notes': v.text(rig.get('notes', ''), '骨骼说明', 2000),
    }


def validate_project_rig(project):
    rig = validate_rig(project.get('rig'), project)
    if rig is None:
        return None
    bone_ids = {bone['id'] for bone in rig['bones']}
    for frame in project['frames']:
        pose = frame.get('pose')
        if pose is None:
            continue
        v.object_value(pose, '关键姿态')
        for field in ('rotations', 'joints'):
            for bone_id, angle in v.object_value(pose.get(field, {}), '关节旋转').items():
                v.key(bone_id)
                if bone_id not in bone_ids:
                    raise v.Invalid('姿态引用了不存在的骨骼')
                v.real(angle, '关节旋转角度', -360, 360)
        root = v.array(pose.get('root', [0, 0]), '整体位移', 2)
        if len(root) != 2:
            raise v.Invalid('整体位移需要两个分量')
        for component in root:
            v.real(component, '整体位移', -4096, 4096)
        for target in v.array(pose.get('targets', []), 'IK 目标', MAX_TARGETS):
            target = v.object_value(target, 'IK 目标')
            if v.key(target.get('bone')) not in bone_ids:
                raise v.Invalid('IK 目标引用了不存在的骨骼')
            v.boolean(target.get('pin', False), '固定脚掌')
            for entry in v.array(target.get('chain', []), 'IK 关节链', MAX_BONES):
                if v.key(entry) not in bone_ids:
                    raise v.Invalid('IK 关节链引用了不存在的骨骼')
            if target.get('target') is not None:
                point = v.array(target['target'], 'IK 目标位置', 2)
                if len(point) != 2:
                    raise v.Invalid('IK 目标需要两个分量')
                for component in point:
                    v.real(component, 'IK 目标位置', -4096, 4096)
    return rig


def parse_pose(pose, rig):
    pose = v.object_value(pose, '关键姿态')
    bones = {bone['id']: bone for bone in rig['bones']}
    rotations = {}
    for bone_id, angle in v.object_value(pose.get('rotations', {}), '关节旋转').items():
        v.key(bone_id)
        if bone_id not in bones:
            raise v.Invalid('姿态引用了不存在的骨骼')
        rotations[bone_id] = v.real(angle, '关节旋转角度', -360, 360)
    root = v.array(pose.get('root', [0, 0]), '整体位移', 2)
    if len(root) != 2:
        raise v.Invalid('整体位移需要两个分量')
    offset = [v.real(root[0], '整体位移X', -4096, 4096), v.real(root[1], '整体位移Y', -4096, 4096)]
    targets = []
    rest = world_frames(bones, {}, (0.0, 0.0))
    for item in v.array(pose.get('targets', []), 'IK 目标', MAX_TARGETS):
        item = v.object_value(item, 'IK 目标')
        bone_id = v.key(item.get('bone'))
        if bone_id not in bones:
            raise v.Invalid('IK 目标引用了不存在的骨骼')
        pin = v.boolean(item.get('pin', False), '固定脚掌')
        point = item.get('target')
        if point is None:
            if not pin:
                raise v.Invalid('IK 目标需要位置，或开启固定脚掌以固定在原位')
            point = [float(rest[bone_id]['tip'][0]), float(rest[bone_id]['tip'][1])]
        point = v.array(point, 'IK 目标位置', 2)
        if len(point) != 2:
            raise v.Invalid('IK 目标需要两个分量')
        point = [v.real(point[0], 'IK 目标X', -4096, 4096), v.real(point[1], 'IK 目标Y', -4096, 4096)]
        chain = item.get('chain')
        if chain is None:
            chain = default_chain(bones, bone_id, point, offset, rig.get('root'))
        chain = [v.key(entry) for entry in v.array(chain, 'IK 关节链', MAX_BONES)]
        if not chain:
            raise v.Invalid('IK 关节链不能为空')
        if chain[-1] != bone_id:
            raise v.Invalid('IK 关节链末端必须是目标骨骼')
        path = ancestry(bones, bone_id)
        if chain != path[-len(chain):]:
            raise v.Invalid('IK 关节链必须是目标骨骼的连续父链')
        targets.append({
            'bone': bone_id,
            'target': point,
            'chain': chain,
            'pin': pin,
        })
    return {'rotations': rotations, 'root': offset, 'targets': targets}


def blend_pose(first, second, factor, rig):
    rotations = {}
    for bone in rig['bones']:
        bone_id = bone['id']
        rotations[bone_id] = first['rotations'].get(bone_id, 0) * (1 - factor) + second['rotations'].get(bone_id, 0) * factor
    root = [first['root'][index] * (1 - factor) + second['root'][index] * factor for index in range(2)]
    targets = []
    for index, item in enumerate(first['targets']):
        if index >= len(second['targets']) or second['targets'][index]['bone'] != item['bone']:
            targets.append(deepcopy(item))
            continue
        other = second['targets'][index]
        targets.append({
            'bone': item['bone'],
            'chain': item['chain'],
            'pin': item['pin'] and other['pin'],
            'target': [item['target'][axis] * (1 - factor) + other['target'][axis] * factor for axis in range(2)],
        })
    return {'rotations': rotations, 'root': root, 'targets': targets}


def solve(project, pose, rig=None):
    rig = validate_project_rig(project) if rig is None else rig
    if rig is None:
        raise v.Invalid('请先建立骨骼与图层绑定')
    parsed = parse_pose(pose, rig)
    bones = {bone['id']: bone for bone in rig['bones']}
    deltas = {}
    clamped = []
    requested = {}
    for bone_id, angle in parsed['rotations'].items():
        requested[bone_id] = round(angle, 6)
        value = clamp_delta(bones[bone_id], angle)
        if abs(value - angle) > 1e-9:
            clamped.append(bone_id)
        deltas[bone_id] = value
    offset = parsed['root']
    chain_bones = {bone_id for item in parsed['targets'] for bone_id in item['chain']}
    for bone_id in list(deltas):
        if bone_id in chain_bones:
            deltas.pop(bone_id)
    reports = [{
        'bone': item['bone'],
        'chain': item['chain'],
        'target': [round(value, 4) for value in item['target']],
        'pin': item['pin'],
        'reached': None,
        'residual': None,
        'iterations': 0,
    } for item in parsed['targets']]
    for item, report in zip(parsed['targets'], reports):
        result = solve_chain(bones, deltas, offset, item['chain'], item['target'], iterations=32)
        report['iterations'] = result['iterations']
    refine_targets(bones, deltas, offset, parsed['targets'])
    frames = world_frames(bones, deltas, offset)
    for report in reports:
        tip = frames[report['bone']]['tip']
        target_point = np.asarray(report['target'], dtype=float)
        report['reached'] = [round(float(tip[0]), 4), round(float(tip[1]), 4)]
        report['residual'] = round(float(np.linalg.norm(tip - target_point)), 4)
    size = (project['width'], project['height'])
    layer_transforms = []
    for binding in rig['bindings']:
        bone = bones[binding['bone']]
        bone_frame = frames[bone['id']]
        posed = joint_matrix(bone_frame['joint'], bone_frame['angle']) @ binding_offset(binding, bone, size)
        layer_transforms.append({
            'bone': bone['id'],
            'layer': binding['layer'],
            'transform': transform_from_matrix(posed, size, binding['bind']),
        })
    explicit = {bone_id: round(clamp_delta(bones[bone_id], angle), 6)
                for bone_id, angle in parsed['rotations'].items()}
    solved_joints = {bone_id: round(value, 6) for bone_id, value in deltas.items() if abs(value) > 1e-9}
    stored = {
        'rotations': explicit,
        'joints': solved_joints,
        'root': [round(offset[0], 6), round(offset[1], 6)],
        'targets': [{
            'bone': item['bone'],
            'chain': item['chain'],
            'pin': item['pin'],
            'target': [round(item['target'][0], 4), round(item['target'][1], 4)],
        } for item in parsed['targets']],
    }
    return {
        'pose': stored,
        'bones': [{
            'id': bone_id,
            'name': bones[bone_id]['name'],
            'parent': bones[bone_id]['parent'],
            'joint': [round(float(frames[bone_id]['joint'][0]), 4), round(float(frames[bone_id]['joint'][1]), 4)],
            'tip': [round(float(frames[bone_id]['tip'][0]), 4), round(float(frames[bone_id]['tip'][1]), 4)],
            'angle': round(normalize_angle(frames[bone_id]['angle']), 4),
            'delta': round(deltas.get(bone_id, 0.0), 4),
            'limit_min': bones[bone_id]['limits']['min'],
            'limit_max': bones[bone_id]['limits']['max'],
            'length': bones[bone_id]['length'],
        } for bone_id in frames],
        'requested_rotations': requested,
        'joints': solved_joints,
        'clamped': clamped,
        'targets': reports,
        'layers': layer_transforms,
        'root': [round(offset[0], 6), round(offset[1], 6)],
    }


def apply_report(frame, report, pose=None):
    lookup = {}
    duplicates = set()
    for layer in walk_layers(frame['layers']):
        if layer['name'] in lookup:
            duplicates.add(layer['name'])
        lookup[layer['name']] = layer
    applied, missing = [], []
    for item in report['layers']:
        name = item['layer']
        if name in duplicates:
            raise v.Invalid('绑定图层名称不唯一：' + name)
        layer = lookup.get(name)
        if layer is None:
            missing.append(name)
            continue
        if layer['locked'] and layer['transform'] != v.transform(item['transform']):
            raise v.Invalid('绑定图层已锁定：' + name)
        layer['transform'] = v.transform(item['transform'])
        applied.append(name)
    if pose is not None:
        frame['pose'] = pose
    return applied, missing


def pose_frame(project, frame_id, pose, rig=None):
    report = solve(project, pose, rig=rig)
    frame = find_frame(project, frame_id)
    applied, missing = apply_report(frame, report, report['pose'])
    report['frame_id'] = frame_id
    report['applied_layers'] = applied
    report['missing_layers'] = missing
    return report


def rest_frame(project, offset=(0.0, 0.0)):
    rig = validate_project_rig(project)
    if rig is None:
        raise v.Invalid('请先建立骨骼与图层绑定')
    bones = {bone['id']: bone for bone in rig['bones']}
    frames = world_frames(bones, {}, offset)
    return frames


def summary(project):
    rig = validate_project_rig(project)
    if rig is None:
        return {'rig': None, 'bones': [], 'bindings': [], 'frames': [
            {'id': frame['id'], 'name': frame['name'], 'has_pose': False} for frame in project['frames']]}
    rest = rest_frame(project)
    bones = []
    for bone in rig['bones']:
        bone_frame = rest[bone['id']]
        bones.append({
            'id': bone['id'],
            'name': bone['name'],
            'parent': bone['parent'],
            'origin': bone['origin'],
            'rest_joint': [round(float(bone_frame['joint'][0]), 4), round(float(bone_frame['joint'][1]), 4)],
            'rest_tip': [round(float(bone_frame['tip'][0]), 4), round(float(bone_frame['tip'][1]), 4)],
            'angle': bone['angle'],
            'length': bone['length'],
            'limit_min': bone['limits']['min'],
            'limit_max': bone['limits']['max'],
            'target': bone['target'],
        })
    return {
        'rig': rig,
        'bones': bones,
        'bindings': rig['bindings'],
        'root': rig['root'],
        'size': rig['size'],
        'notes': rig['notes'],
        'frames': [{'id': frame['id'], 'name': frame['name'], 'has_pose': 'pose' in frame,
                    'pose': frame.get('pose')} for frame in project['frames']],
    }


def auto_rig(store, project, frame_id, names=None, root_name='根'):
    """Build a start-from-parts rig from one frame's layer pixels.

    The largest visible part becomes the torso directly under the root, and
    every other part hangs off the torso, which gives a body-first hierarchy
    (moving the body carries the limbs) that can be edited bone by bone.
    """
    from .composite import trim_bounds
    from .role_tools import display_image
    frame = find_frame(project, frame_id)
    if names is None:
        names = [layer['name'] for layer in frame['layers']
                 if layer.get('kind') != 'group' and layer['visible']]
    unique = []
    for name in names:
        if name not in unique:
            unique.append(name)
    if not unique:
        raise v.Invalid('没有可绑定的图层')
    if len(unique) > MAX_BINDINGS:
        raise v.Invalid('图层绑定数量超限')
    matches = {}
    for layer in walk_layers(frame['layers']):
        matches.setdefault(layer['name'], []).append(layer)
    if any(len(matches[name]) > 1 for name in unique):
        raise v.Invalid('图层名称不唯一，无法自动绑定；请先重命名')
    bounds = {}
    for name in unique:
        image = display_image(store, project, matches[name][0])
        box = trim_bounds(image)
        bounds[name] = box or [0, 0, project['width'], project['height']]
    def area(name):
        left, top, right, bottom = bounds[name]
        return (right - left) * (bottom - top)
    torso = max(unique, key=area)
    union_left = min(bounds[name][0] for name in unique)
    union_top = min(bounds[name][1] for name in unique)
    union_right = max(bounds[name][2] for name in unique)
    union_bottom = max(bounds[name][3] for name in unique)
    root_bone = {
        'id': new_id(),
        'name': v.text(root_name, '根骨骼名称', 60, False),
        'parent': None,
        'origin': [(union_left + union_right) / 2, union_bottom],
        'angle': -90,
        'length': max(2, (union_bottom - union_top) / 3),
        'limit_min': -180,
        'limit_max': 180,
        'target': False,
    }
    bones = [root_bone]
    bindings = []
    def add(name, parent):
        left, top, right, bottom = bounds[name]
        bone = {
            'id': new_id(),
            'name': name,
            'parent': parent,
            'origin': [(left + right) / 2, bottom],
            'angle': -90,
            'length': max(2, (bottom - top) / 2),
            'limit_min': -180,
            'limit_max': 180,
            'target': name != torso,
        }
        bones.append(bone)
        bindings.append({'bone': bone['id'], 'layer': name, 'bind': v.transform(matches[name][0]['transform'])})
        return bone['id']
    torso_bone = add(torso, root_bone['id'])
    for name in unique:
        if name != torso:
            add(name, torso_bone)
    return {
        'bones': bones,
        'bindings': bindings,
        'root': root_bone['id'],
        'created_frame': frame_id,
        'size': [project['width'], project['height']],
        'notes': '自动建立：最大可见图层作为躯干，其余图层挂在躯干下；关节位置、活动范围与 IK 目标请在面板中核对。',
    }


def rebase_geometry(project, point, source_size, target_size, scale=1.0, angle_delta=0.0):
    """Re-base an existing rig after a canvas geometry operation.

    Geometry tools bake every layer transform into pixels, so the binding
    transforms and frame poses no longer describe the drawn pixels; the
    bindings are re-captured as the baked state and stored frame poses are
    dropped (their result is already in the pixels).  Bone joints are moved
    through ``point`` and lengths/angles follow the same scale and rotation.
    """
    rig = project.get('rig')
    if rig is None:
        return
    for bone in rig['bones']:
        moved = point(bone['origin'])
        bone['origin'] = [round(float(moved[0]), 4), round(float(moved[1]), 4)]
        bone['length'] = round(max(0.5, float(bone['length']) * scale), 4)
        bone['angle'] = round(normalize_angle(float(bone['angle']) + angle_delta), 4)
    rig['size'] = [int(target_size[0]), int(target_size[1])]
    for binding in rig.get('bindings', []):
        binding['bind'] = v.transform({})
    for frame in project['frames']:
        frame.pop('pose', None)


class Rig:
    def __init__(self, store):
        self.store = store

    def state(self, project_id):
        return summary(self.store.get(project_id))

    def save(self, project_id, revision, body):
        rig = body.get('rig', body)
        bind_frame = rig.get('created_frame') or body.get('frame_id')
        if bind_frame:
            v.key(bind_frame)
            find_frame(self.store.get(project_id), bind_frame)
        with self.store.edit(project_id, revision, '保存骨骼与图层绑定') as project:
            bones, root = parse_bones(rig, project)
            bindings = parse_bindings({'bones': bones, 'bindings': rig.get('bindings', [])}, project, bind_frame)
            project['rig'] = validate_rig({
                'bones': bones,
                'bindings': bindings,
                'root': root,
                'created_frame': bind_frame,
                'size': [project['width'], project['height']],
                'notes': rig.get('notes', ''),
            }, project)
        return self.store.get(project_id)

    def auto(self, project_id, revision, body):
        frame_id = body.get('frame_id')
        with self.store.lock:
            project = self.store.get(project_id)
            rig = auto_rig(self.store, project, frame_id, body.get('layers'), body.get('root_name', '根'))
        return self.save(project_id, revision, {'rig': rig})

    def remove(self, project_id, revision):
        with self.store.edit(project_id, revision, '移除骨骼绑定') as project:
            rig = project.pop('rig', None)
            if rig is None:
                raise v.Invalid('项目没有骨骼绑定')
            for frame in project['frames']:
                frame.pop('pose', None)
        return self.store.get(project_id)

    def solve(self, project_id, body):
        project = self.store.get(project_id)
        return solve(project, body.get('pose', body))

    def preview(self, project_id, body):
        from .composite import render_frame, png_bytes
        frame_id = body.get('frame_id')
        with self.store.lock:
            project = deepcopy(self.store.get(project_id))
            frame = find_frame(project, frame_id)
            pose = body.get('pose')
            rig = validate_project_rig(project)
            if rig is None:
                raise v.Invalid('请先建立骨骼与图层绑定')
            if pose is None:
                pose = frame.get('pose', {'rotations': {}, 'root': [0, 0], 'targets': []})
            report = pose_frame(project, frame_id, pose, rig=rig)
            image = render_frame(self.store, project, frame_id)
        return report, image

    def apply(self, project_id, revision, body):
        frame_id = body.get('frame_id')
        with self.store.edit(project_id, revision, '调整角色姿态') as project:
            report = pose_frame(project, frame_id, body.get('pose', body))
        result = self.store.get(project_id)
        return {'project': result, 'report': report}

    def clear(self, project_id, revision, frame_id):
        with self.store.edit(project_id, revision, '清除角色姿态') as project:
            rig = validate_project_rig(project)
            if rig is None:
                raise v.Invalid('项目没有骨骼绑定')
            frame = find_frame(project, frame_id)
            report = {
                'layers': [{'bone': binding['bone'], 'layer': binding['layer'], 'transform': binding['bind']}
                           for binding in rig['bindings']],
            }
            apply_report(frame, report)
            frame.pop('pose', None)
        return self.store.get(project_id)

    def keyframes(self, project_id, revision, body):
        frame_id = body.get('frame_id')
        with self.store.edit(project_id, revision, '生成关键姿态帧') as project:
            rig = validate_project_rig(project)
            if rig is None:
                raise v.Invalid('请先建立骨骼绑定')
            source = find_frame(project, frame_id)
            raw = v.array(body.get('poses', []), '关键姿态', 30)
            between = v.integer(body.get('between', 0), '姿态间过渡帧数', 0, 30)
            duration = v.duration(body.get('duration', source['duration']))
            prefix = v.text(body.get('name', '姿态'), '姿态帧名称', 40, False)
            poses = [parse_pose(item, rig) for item in raw]
            if not poses:
                poses = [parse_pose(source.get('pose') or {'rotations': {}, 'root': [0, 0]}, rig)]
            sequences = []
            for index, pose in enumerate(poses):
                if index:
                    for step in range(1, between + 1):
                        sequences.append((f'{prefix} {index}-{step}/{between}',
                                          blend_pose(poses[index - 1], pose, step / (between + 1), rig)))
                sequences.append((f'{prefix} {index + 1}/{len(poses)}', pose))
            if len(project['frames']) + len(sequences) > 300:
                raise v.Invalid('生成姿态帧后超过300帧')
            additions = []
            for name, pose in sequences:
                copied = deepcopy(source)
                copied['id'] = new_id()
                for layer in walk_layers(copied['layers']):
                    layer['id'] = new_id()
                copied['name'] = name[:100]
                copied['duration'] = duration
                solved = solve(project, pose, rig=rig)
                apply_report(copied, solved, solved['pose'])
                copied['pose_residual'] = [item['residual'] for item in solved['targets']]
                if solved['targets']:
                    line = 'IK 残差：' + ', '.join(f'{item["residual"]:.2f}px' for item in solved['targets'])
                    copied['notes'] = (copied['notes'] + '\n' + line).strip()
                additions.append(copied)
            index = project['frames'].index(source) + 1
            project['frames'][index:index] = additions
        return self.store.get(project_id)
