"""Reversible floating selections.

A try-out session freezes the layer pixels at the moment the selection starts.
All previews and the single confirmation are rebuilt from those frozen pixels,
the recorded (add/subtract) selection mask and the current try-out transform,
so repeated moves, rotations and scales never compound.  Nothing is written to
the project revision until the user confirms exactly once.

Coverage model
--------------
The soft mask ``m`` partitions the straight-alpha layer into a source residue
``A * (1 - m)`` that stays in place and a floating fragment ``A * m`` that is
moved.  The two parts always sum back to the original coverage, therefore a
zero transform restores semi-transparent pixels exactly.  At a destination
pixel the fragment coverage is added to the residue but capped by
``max(destination alpha, fragment alpha)``: moving a fragment across its own
soft edge (or across other content) can never stack alpha and darken the edge.
"""
import os
from copy import deepcopy
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from . import validate as v
from .model import editable_layer
from .layer_nodes import require_leaf


SHAPES = ('lasso', 'rectangle', 'ellipse')
OPERATIONS = ('add', 'subtract', 'replace')
MAX_FEATHER = 64
MAX_POINTS = 4000
MAX_OPS = 64
POINT_LIMIT = 8192
OFFSET_LIMIT = 8192
MIN_SCALE = .05
MAX_SCALE = 8
ASSET_PIXELS = 4194304
NEUTRAL = {'x': 0, 'y': 0, 'rotation': 0, 'scale_x': 1, 'scale_y': 1, 'flip_x': False, 'flip_y': False}


def _alias(value, options, name, default):
    if value is None:
        return default
    if not isinstance(value, str):
        raise v.Invalid(name + '必须是文本')
    value = value.strip().lower()
    if value not in options:
        raise v.Invalid(name + '取值不支持')
    return value


def _points(value, name):
    result = []
    for item in v.array(value, name, MAX_POINTS):
        if isinstance(item, dict):
            x, y = item.get('x'), item.get('y')
        else:
            pair = v.array(item, name, 2)
            if len(pair) != 2:
                raise v.Invalid(name + '顶点需要x和y')
            x, y = pair
        result.append([v.real(x, name + 'X', -POINT_LIMIT, POINT_LIMIT),
                       v.real(y, name + 'Y', -POINT_LIMIT, POINT_LIMIT)])
    if len(result) < 3:
        raise v.Invalid('套索至少需要三个顶点')
    return result


def _box(body):
    source = body.get('box') if isinstance(body.get('box'), dict) else body
    return {
        'x': v.real(source.get('x', 0), '选区X', -POINT_LIMIT, POINT_LIMIT),
        'y': v.real(source.get('y', 0), '选区Y', -POINT_LIMIT, POINT_LIMIT),
        'width': v.real(source.get('width', source.get('w')), '选区宽度', 1, 2 * POINT_LIMIT),
        'height': v.real(source.get('height', source.get('h')), '选区高度', 1, 2 * POINT_LIMIT),
    }


def selection_op(body):
    body = v.object_value(body, '选区操作')
    shape = _alias(body.get('shape', body.get('type')), SHAPES, '选区形状', 'rectangle')
    operation = _alias(body.get('operation'), OPERATIONS, '选区运算', 'replace')
    op = {
        'shape': shape,
        'operation': operation,
        'feather': v.real(body.get('feather', 0), '软边半径', 0, MAX_FEATHER),
    }
    if shape == 'lasso':
        op['points'] = _points(body.get('points'), '套索顶点')
    else:
        op['box'] = _box(body)
    return op


def initial_ops(body):
    """Accept a single operation, an ``ops`` list or a ``selection`` list."""
    body = v.object_value(body, '浮动选区参数')
    for key in ('ops', 'selection_ops', 'regions'):
        if key in body:
            ops = [selection_op(item) for item in v.array(body[key], '选区操作', MAX_OPS)]
            if not ops:
                raise v.Invalid('至少需要一个选区操作')
            return ops
    if 'selection' in body and isinstance(body['selection'], list):
        ops = [selection_op(item) for item in v.array(body['selection'], '选区操作', MAX_OPS)]
        if not ops:
            raise v.Invalid('至少需要一个选区操作')
        return ops
    source = body.get('selection') if isinstance(body.get('selection'), dict) else body
    return [selection_op(source)]


def transform_value(body, base=None, allow_xy=True):
    """Read a transform patch; missing fields keep the previous value."""
    source = body.get('transform') if isinstance(body.get('transform'), dict) else body
    current = dict(base or NEUTRAL)
    x = source.get('x', source.get('dx', source.get('offset_x')))
    y = source.get('y', source.get('dy', source.get('offset_y')))
    if not allow_xy:
        x = source.get('dx', source.get('offset_x'))
        y = source.get('dy', source.get('offset_y'))
    rotation = source.get('rotation', source.get('angle'))
    scale = source.get('scale')
    scale_x = source.get('scale_x', scale)
    scale_y = source.get('scale_y', scale)
    result = dict(current)
    if x is not None:
        result['x'] = v.integer(x, '浮动水平偏移', -OFFSET_LIMIT, OFFSET_LIMIT)
    if y is not None:
        result['y'] = v.integer(y, '浮动垂直偏移', -OFFSET_LIMIT, OFFSET_LIMIT)
    if rotation is not None:
        result['rotation'] = v.real(rotation, '浮动旋转角度', -360, 360)
    if scale_x is not None:
        result['scale_x'] = v.real(scale_x, '浮动横向缩放', MIN_SCALE, MAX_SCALE)
    if scale_y is not None:
        result['scale_y'] = v.real(scale_y, '浮动纵向缩放', MIN_SCALE, MAX_SCALE)
    if 'flip_x' in source:
        result['flip_x'] = v.boolean(source['flip_x'], '浮动水平翻转')
    if 'flip_y' in source:
        result['flip_y'] = v.boolean(source['flip_y'], '浮动垂直翻转')
    return result


def copy_value(body, fallback=False):
    if 'copy' not in body:
        return fallback
    return v.boolean(body['copy'], '复制而非移动')


def is_neutral(transform):
    return (transform['x'] == 0 and transform['y'] == 0
            and abs(transform['rotation'] % 360) < 1e-9
            and abs(transform['scale_x'] - 1) < 1e-9
            and abs(transform['scale_y'] - 1) < 1e-9
            and not transform['flip_x'] and not transform['flip_y'])


def _shape_mask(op, size):
    mask = Image.new('L', size, 0)
    painter = ImageDraw.Draw(mask)
    if op['shape'] == 'lasso':
        painter.polygon([tuple(p) for p in op['points']], fill=255)
    else:
        box = op['box']
        area = (box['x'], box['y'], box['x'] + box['width'] - 1, box['y'] + box['height'] - 1)
        (painter.ellipse if op['shape'] == 'ellipse' else painter.rectangle)(area, fill=255)
    if op['feather'] > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(op['feather']))
    return mask


def build_mask(ops, size):
    if not ops:
        raise v.Invalid('尚未建立选区')
    mask = Image.new('L', size, 0)
    for op in ops:
        shape = np.asarray(_shape_mask(op, size), dtype=np.int64)
        current = np.asarray(mask, dtype=np.int64)
        if op['operation'] == 'replace':
            combined = shape
        elif op['operation'] == 'add':
            combined = np.maximum(current, shape)
        else:
            combined = (current * (255 - shape) + 127) // 255
        mask = Image.fromarray(combined.astype(np.uint8))
    if mask.getbbox() is None:
        raise v.Invalid('选区为空，请继续套索或矩形添加区域')
    return mask


def mask_image(mask):
    image = Image.new('RGBA', mask.size, (255, 255, 255, 0))
    image.putalpha(mask.convert('L'))
    return image


def _transform_fragment(fragment, transform):
    image = fragment
    if transform['flip_x']:
        image = ImageOps.mirror(image)
    if transform['flip_y']:
        image = ImageOps.flip(image)
    width = max(1, int(round(image.width * transform['scale_x'])))
    height = max(1, int(round(image.height * transform['scale_y'])))
    if width * height > ASSET_PIXELS:
        raise v.Invalid('浮动片段变换后的尺寸过大')
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.NEAREST)
    if transform['rotation']:
        image = image.rotate(-transform['rotation'], resample=Image.Resampling.NEAREST, expand=True)
    return image


def _placement(bounds, size, transform):
    left, top, right, bottom = bounds
    return (int(round((left + right) / 2 + transform['x'] - size[0] / 2)),
            int(round((top + bottom) / 2 + transform['y'] - size[1] / 2)))


def compose(base_image, mask, transform, copy=False):
    """Rebuild the layer from frozen pixels, mask and try-out transform.

    ``copy=True`` keeps the source pixels in place and stamps the fragment on
    top instead of cutting it away.
    """
    base_image = base_image.convert('RGBA')
    mask = mask.convert('L')
    if base_image.size != mask.size:
        raise v.Invalid('选区遮罩尺寸必须与图层一致')
    if mask.getbbox() is None:
        raise v.Invalid('选区为空')
    if is_neutral(transform) and not copy:
        return base_image.copy()
    base = np.asarray(base_image, dtype=np.int64)
    selected = np.asarray(mask, dtype=np.int64)
    alpha = base[:, :, 3]
    fragment_alpha = (alpha * selected + 127) // 255
    residue_alpha = alpha.copy() if copy else alpha - fragment_alpha
    bounds = mask.getbbox()
    fragment = np.asarray(base_image.crop(bounds), dtype=np.int64).copy()
    fragment[:, :, 3] = fragment_alpha[bounds[1]:bounds[3], bounds[0]:bounds[2]]
    moved = _transform_fragment(Image.fromarray(fragment.astype(np.uint8)), transform)
    offset_x, offset_y = _placement(bounds, moved.size, transform)
    width, height = base_image.size
    left, top = max(offset_x, 0), max(offset_y, 0)
    right, bottom = min(offset_x + moved.width, width), min(offset_y + moved.height, height)
    coverage = residue_alpha.copy()
    weighted = residue_alpha[:, :, None] * base[:, :, :3]
    cap = alpha.copy()
    if left < right and top < bottom:
        source = np.asarray(moved, dtype=np.int64)
        piece = source[top - offset_y:top - offset_y + (bottom - top), left - offset_x:left - offset_x + (right - left)]
        piece_alpha = piece[:, :, 3]
        coverage[top:bottom, left:right] += piece_alpha
        weighted[top:bottom, left:right] += piece_alpha[:, :, None] * piece[:, :, :3]
        cap[top:bottom, left:right] = np.maximum(cap[top:bottom, left:right], piece_alpha)
    output_alpha = np.minimum(coverage, cap)
    output_alpha = np.clip(output_alpha, 0, 255)
    total = coverage.astype(float)
    color = np.divide(weighted, total[:, :, None], out=np.zeros(weighted.shape, dtype=float), where=total[:, :, None] > 0)
    result = np.concatenate([np.rint(color), output_alpha[:, :, None]], axis=2)
    hidden = (output_alpha == 0) & (alpha == 0)
    result[hidden, :3] = base[hidden, :3]
    return Image.fromarray(np.clip(result, 0, 255).astype(np.uint8))


def _directory(store, project_id):
    path = store.directory(project_id) / 'floating'
    path.mkdir(exist_ok=True)
    return path


def _session_path(store, project_id, session_id):
    v.key(session_id)
    return _directory(store, project_id) / (session_id + '.json')


def _save(store, project_id, session):
    store.write_json(_session_path(store, project_id, session['id']), session)


def _read(store, project_id, session_id):
    path = _session_path(store, project_id, session_id)
    if not path.exists():
        raise v.Invalid('浮动选区不存在或已确认/取消')
    session = store.read_json(path)
    v.object_value(session, '浮动选区')
    v.key(session.get('id'))
    v.key(session.get('frame_id'))
    v.key(session.get('layer_id'))
    v.digest(session.get('base_asset'))
    v.digest(session.get('mask_asset'))
    if session['id'] != session_id:
        raise v.Invalid('浮动选区编号不一致')
    return session


def read_session(store, project_id, session_id):
    return _read(store, project_id, session_id)


def require_basis(project, frame_id, layer_id, session):
    return _require_basis(project, frame_id, layer_id, session)


def _drop(store, project_id, session_id):
    _session_path(store, project_id, session_id).unlink(missing_ok=True)


def _drop_layer_sessions(store, project_id, frame_id, layer_id, keep):
    for path in _directory(store, project_id).glob('*.json'):
        try:
            other = store.read_json(path)
        except (v.Invalid, ValueError, OSError):
            continue
        if other.get('frame_id') == frame_id and other.get('layer_id') == layer_id and other.get('id') != keep:
            path.unlink(missing_ok=True)


def _require_basis(project, frame_id, layer_id, session):
    frame, layer = editable_layer(project, frame_id, layer_id)
    if layer['id'] != session['layer_id'] or frame['id'] != session['frame_id']:
        raise v.Invalid('浮动选区与当前图层不匹配')
    if layer['asset'] != session['base_asset']:
        raise v.Conflict('浮动选区依据的像素已被修改，请取消后重新提取')
    require_leaf(layer)
    from .role_tools import require_ordinary
    require_ordinary(layer)
    from .cels import require_detached
    require_detached(layer)
    if layer.get('affine') or layer['transform'] != v.transform({}):
        raise v.Invalid('浮动选区按图层原始坐标工作，请先烘焙图层变换')
    return layer


def mask_of(store, project_id, session):
    return store.image(project_id, session['mask_asset']).getchannel('A')


def _refresh(store, project_id, session, ops):
    project = store.get(project_id)
    mask = build_mask(ops, (project['width'], project['height']))
    session['selection'] = ops
    session['mask_asset'] = store.put_image(project_id, mask_image(mask))
    session['bounds'] = list(mask.getbbox())
    session['masked_pixels'] = int(np.count_nonzero(np.asarray(mask)))
    session['effective_coverage'] = round(float(np.asarray(mask, dtype=float).sum() / 255), 4)
    base = np.asarray(store.image(project_id, session['base_asset']))
    covered = np.asarray(mask, dtype=np.int64) > 0
    captured = base[:, :, 3][covered]
    session['captured_pixels'] = int(captured.size)
    session['captured_empty'] = bool(captured.size and not np.any(captured))
    return session


def summary(session):
    return {
        'id': session['id'],
        'frame_id': session['frame_id'],
        'layer_id': session['layer_id'],
        'base_asset': session['base_asset'],
        'base_revision': session.get('base_revision'),
        'bounds': session.get('bounds'),
        'masked_pixels': session.get('masked_pixels'),
        'effective_coverage': session.get('effective_coverage'),
        'captured_pixels': session.get('captured_pixels'),
        'captured_empty': session.get('captured_empty'),
        'transform': deepcopy(session['transform']),
        'copy': bool(session.get('copy')),
        'selection': deepcopy(session.get('selection', [])),
        'created_at': session.get('created_at'),
    }


def list_sessions(store, project_id, frame_id=None, layer_id=None):
    with store.lock:
        project = store.get(project_id)
        from .layer_nodes import walk_layers
        assets = {}
        for frame in project['frames']:
            for layer in walk_layers(frame['layers']):
                assets[layer['id']] = layer['asset']
        sessions = []
        for path in sorted(_directory(store, project_id).glob('*.json')):
            try:
                session = store.read_json(path)
                if session.get('layer_id') not in assets or assets[session['layer_id']] != session.get('base_asset'):
                    path.unlink(missing_ok=True)
                    continue
                if frame_id and session.get('frame_id') != frame_id:
                    continue
                if layer_id and session.get('layer_id') != layer_id:
                    continue
                sessions.append(summary(session))
            except (v.Invalid, KeyError, TypeError, OSError):
                continue
        return sessions


def start(store, project_id, frame_id, layer_id, revision, body):
    ops = initial_ops(body)
    transform = transform_value(body, allow_xy=isinstance(body.get('transform'), dict))
    with store.lock:
        project = store.get(project_id)
        if v.revision(body) != project['revision']:
            raise v.Conflict('项目已被其他操作修改，请刷新后重试')
        _, layer = editable_layer(project, frame_id, layer_id)
        require_leaf(layer)
        from .role_tools import require_ordinary
        require_ordinary(layer)
        from .cels import require_detached
        require_detached(layer)
        if layer.get('affine') or layer['transform'] != v.transform({}):
            raise v.Invalid('浮动选区按图层原始坐标工作，请先烘焙图层变换')
        session = {
            'id': os.urandom(16).hex(),
            'frame_id': frame_id,
            'layer_id': layer_id,
            'base_asset': layer['asset'],
            'base_revision': project['revision'],
            'created_at': v.timestamp(),
            'transform': transform,
            'copy': copy_value(body),
        }
        _refresh(store, project_id, session, ops)
        _save(store, project_id, session)
        _drop_layer_sessions(store, project_id, frame_id, layer_id, session['id'])
        return summary(session)


def add_mask(store, project_id, frame_id, layer_id, session_id, body):
    op = selection_op(body)
    with store.lock:
        project = store.get(project_id)
        session = _read(store, project_id, session_id)
        _require_basis(project, frame_id, layer_id, session)
        ops = list(session.get('selection', []))
        if op['operation'] == 'replace':
            ops = []
        elif len(ops) >= MAX_OPS:
            raise v.Invalid('最多叠加' + str(MAX_OPS) + '次选区运算，请重画选区')
        ops.append(op)
        _refresh(store, project_id, session, ops)
        _save(store, project_id, session)
        return summary(session)


def update_transform(store, project_id, frame_id, layer_id, session_id, body):
    with store.lock:
        project = store.get(project_id)
        session = _read(store, project_id, session_id)
        _require_basis(project, frame_id, layer_id, session)
        session['transform'] = transform_value(body, session['transform'])
        session['copy'] = copy_value(body, session.get('copy', False))
        _save(store, project_id, session)
        return summary(session)


def floating_layer(store, project_id, session):
    base = store.image(project_id, session['base_asset'])
    return compose(base, mask_of(store, project_id, session), session['transform'], session.get('copy', False))


def preview(store, project_id, frame_id, layer_id, session_id, whole_frame=False):
    with store.lock:
        project = store.get(project_id)
        session = _read(store, project_id, session_id)
        layer = _require_basis(project, frame_id, layer_id, session)
        image = floating_layer(store, project_id, session)
        if not whole_frame:
            return image
        from .composite import render_frame
        return render_frame(store, project, frame_id, overrides={layer['id']: image})


def preview_mask(store, project_id, frame_id, layer_id, session_id):
    with store.lock:
        project = store.get(project_id)
        session = _read(store, project_id, session_id)
        _require_basis(project, frame_id, layer_id, session)
        return mask_of(store, project_id, session)


def confirm(store, project_id, frame_id, layer_id, session_id, revision):
    with store.lock:
        session = _read(store, project_id, session_id)
        with store.edit(project_id, revision, '确认浮动选区') as project:
            layer = _require_basis(project, frame_id, layer_id, session)
            neutral = is_neutral(session['transform'])
            if neutral and not session.get('copy'):
                layer['asset'] = session['base_asset']
            else:
                layer['asset'] = store.put_image(project_id, floating_layer(store, project_id, session))
        _drop(store, project_id, session_id)
        return store.get(project_id)


def cancel(store, project_id, frame_id, layer_id, session_id):
    with store.lock:
        session = _read(store, project_id, session_id)
        if session['frame_id'] != frame_id or session['layer_id'] != layer_id:
            raise v.Invalid('浮动选区与当前图层不匹配')
        _drop(store, project_id, session_id)
        return store.get(project_id)
