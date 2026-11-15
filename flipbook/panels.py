"""Layered, resizable panel workspaces rebuilt from pristine source layers.

A panel document describes a 9-slice style grid (fixed / stretch / repeat
columns and rows) and decorations that attach to panel edges or to another
decoration.  Every resize is recomputed from the original full-canvas source
layer pixels, so fixed corner ornaments are never stretched and repeated
resizing cannot accumulate resampling damage.  Each source layer is mapped
independently and stays an independently editable layer in the output.
"""
from . import validate as v
from .model import new_id
from .layer_nodes import walk_layers

GRID_BEHAVIORS = ('fixed', 'stretch', 'repeat')
EDGE_ANCHORS = (
    'top_left', 'top', 'top_right', 'left', 'center', 'right',
    'bottom_left', 'bottom', 'bottom_right',
)
ALIGNMENTS = ('start', 'center', 'end')
MAX_DECORATIONS = 40


def workspaces(project):
    """All panel workspaces embedded in the project's frames."""
    return [frame['panel_workspace'] for frame in project.get('frames', [])
            if isinstance(frame.get('panel_workspace'), dict)]


def source_assets(project):
    """Map of panel source asset -> original full-canvas size (exempt from
    the ordinary canvas-size asset check)."""
    result = {}
    for frame in project.get('frames', []):
        workspace = frame.get('panel_workspace')
        if not workspace:
            continue
        size = tuple(workspace['source_size'])
        for layer in walk_layers(frame['layers']):
            meta = layer.get('panel_source')
            if meta and meta.get('asset'):
                result[meta['asset']] = size
    return result


def _grid_entries(raw, axis, source_extent, name):
    entries = v.array(raw, name, 12)
    if not entries:
        raise v.Invalid(name + '至少需要一段')
    parsed = []
    stretch_count = 0
    used = 0
    for index, entry in enumerate(entries):
        entry = v.object_value(entry, name)
        behavior = v.choice(entry.get('behavior', 'fixed'), name + '伸缩方式', GRID_BEHAVIORS)
        span = v.integer(entry.get('span', 1), name + '段宽', 1, source_extent)
        if behavior == 'stretch':
            stretch_count += 1
        if span > source_extent - used:
            raise v.Invalid(name + '段尺寸超出原稿范围')
        used += span
        parsed.append({'id': entry.get('id') or f'{axis}-{index}', 'behavior': behavior, 'span': span})
    if used != source_extent:
        raise v.Invalid(name + '各段尺寸之和必须等于原稿尺寸')
    if stretch_count > 1:
        raise v.Invalid(name + '最多只能有一段拉伸区域')
    return parsed


def _decoration(raw, known):
    entry = v.object_value(raw, '装饰')
    identifier = entry.get('id') or new_id()
    if isinstance(identifier, str) and len(identifier) == 32:
        v.key(identifier)
    else:
        identifier = v.text(identifier, '装饰编号', 60, False)
    rect_raw = v.array(entry.get('rect'), '装饰区域', 4)
    if len(rect_raw) != 4:
        raise v.Invalid('装饰区域需要 [x, y, 宽, 高] 四个整数')
    decoration = {
        'id': identifier,
        'name': v.text(entry.get('name', '装饰'), '装饰名称', 80, False),
        'rect': [
            v.integer(rect_raw[0], '装饰区域X', 0),
            v.integer(rect_raw[1], '装饰区域Y', 0),
            v.integer(rect_raw[2], '装饰区域宽', 1),
            v.integer(rect_raw[3], '装饰区域高', 1),
        ],
    }
    attachment = v.object_value(entry.get('attachment', {}), '附着关系')
    target = attachment.get('target', 'edge')
    attached = {
        'anchor': v.choice(attachment.get('anchor', 'top_left'), '附着锚点', EDGE_ANCHORS),
        'x_align': v.choice(attachment.get('x_align', 'start'), '横向对齐', ALIGNMENTS),
        'y_align': v.choice(attachment.get('y_align', 'start'), '纵向对齐', ALIGNMENTS),
        'offset_x': v.integer(attachment.get('offset_x', 0), '附着偏移X', -2048, 2048),
        'offset_y': v.integer(attachment.get('offset_y', 0), '附着偏移Y', -2048, 2048),
    }
    if target == 'edge':
        attached['target'] = 'edge'
    else:
        if isinstance(target, str) and len(target) == 32:
            v.key(target)
        else:
            target = v.text(target, '附着目标', 60, False)
        if target not in known:
            raise v.Invalid('装饰附着目标不存在：' + str(target))
        if target == identifier:
            raise v.Invalid('装饰不能附着于自身')
        attached['target'] = target
    decoration['attachment'] = attached
    gap_raw = v.array(entry.get('min_gap', [0, 0, 0, 0]), '最小间距', 4)
    if len(gap_raw) != 4:
        raise v.Invalid('最小间距需要 [左, 上, 右, 下] 四个整数')
    decoration['min_gap'] = [v.integer(value, '最小间距', 0, 2048) for value in gap_raw]
    decoration['center_x'] = v.boolean(entry.get('center_x', False), '横向居中')
    decoration['center_y'] = v.boolean(entry.get('center_y', False), '纵向居中')
    return decoration


def validate_workspace(workspace):
    source_w, source_h = workspace['source_size']
    _grid_entries(workspace['grid']['columns'], 'x', source_w, '列网格')
    _grid_entries(workspace['grid']['rows'], 'y', source_h, '行网格')
    known = set()
    for raw in v.array(workspace.get('decorations', []), '装饰', MAX_DECORATIONS):
        decoration = _decoration(raw, known | {raw.get('id')})
        if decoration['id'] in known:
            raise v.Invalid('装饰编号重复')
        x, y, w, h = decoration['rect']
        if x + w > source_w or y + h > source_h:
            raise v.Invalid('装饰「%s」区域超出原稿画布' % decoration['name'])
        known.add(decoration['id'])
    # attachment chains must terminate at an edge and contain no cycle.
    by_id = {item['id']: item for item in workspace.get('decorations', [])}
    for decoration in workspace.get('decorations', []):
        seen = set()
        current = decoration
        while current['attachment']['target'] != 'edge':
            if current['id'] in seen:
                raise v.Invalid('装饰附着关系存在循环')
            seen.add(current['id'])
            current = by_id[current['attachment']['target']]


def validate_document(project):
    for workspace in workspaces(project):
        validate_workspace(workspace)


def default_grid(source_w, source_h, margins):
    left, right, top, bottom = margins
    return {
        'columns': [
            {'id': 'col-left', 'behavior': 'fixed', 'span': left},
            {'id': 'col-fill', 'behavior': 'stretch', 'span': source_w - left - right},
            {'id': 'col-right', 'behavior': 'fixed', 'span': right},
        ],
        'rows': [
            {'id': 'row-top', 'behavior': 'fixed', 'span': top},
            {'id': 'row-fill', 'behavior': 'stretch', 'span': source_h - top - bottom},
            {'id': 'row-bottom', 'behavior': 'fixed', 'span': bottom},
        ],
    }


def validate_margins(source_w, source_h, margins):
    left = v.integer(margins.get('left', 8), '左边框', 0, source_w - 1)
    right = v.integer(margins.get('right', 8), '右边框', 0, source_w - 1)
    top = v.integer(margins.get('top', 8), '上边框', 0, source_h - 1)
    bottom = v.integer(margins.get('bottom', 8), '下边框', 0, source_h - 1)
    if left + right >= source_w or top + bottom >= source_h:
        raise v.Invalid('固定边饰总和必须小于原稿尺寸')
    return left, right, top, bottom


def _axis_bounds(entries, target_extent):
    """Map grid segments to source and target pixel boundaries.

    Fixed segments keep their source span exactly (corner ornaments are
    never stretched); the single stretch segment absorbs all slack; repeat
    segments keep their source span and are tiled by the rasterizer.
    """
    source_bounds = [0]
    for entry in entries:
        source_bounds.append(source_bounds[-1] + entry['span'])
    fixed_total = sum(entry['span'] for entry in entries if entry['behavior'] == 'fixed')
    repeat_total = sum(entry['span'] for entry in entries if entry['behavior'] == 'repeat')
    stretch = next((entry for entry in entries if entry['behavior'] == 'stretch'), None)
    flexible_count = sum(1 for entry in entries if entry['behavior'] != 'fixed')
    if target_extent < fixed_total + flexible_count:
        raise v.Invalid('目标尺寸不足以容纳固定边饰，至少需要 %d 像素'
                        % (fixed_total + flexible_count))
    if stretch is None:
        # Without a stretch segment nothing can absorb slack, so the target
        # extent has to match the fixed and repeat segments exactly.
        if target_extent != fixed_total + repeat_total:
            raise v.Invalid('该轴没有拉伸区域，目标尺寸必须等于固定与平铺段之和 %d'
                            % (fixed_total + repeat_total))
        stretch_span = 0
    else:
        stretch_span = target_extent - fixed_total - repeat_total
        if stretch_span < 1:
            raise v.Invalid('目标尺寸过小，拉伸区域没有可用空间')
    target_bounds = [0]
    for entry in entries:
        if entry['behavior'] == 'fixed':
            target_bounds.append(target_bounds[-1] + entry['span'])
        elif entry['behavior'] == 'repeat':
            target_bounds.append(target_bounds[-1] + entry['span'])
        else:
            target_bounds.append(target_bounds[-1] + stretch_span)
    return source_bounds, target_bounds


def grid_mapping(workspace, width, height):
    columns = workspace['grid']['columns']
    rows = workspace['grid']['rows']
    col_source, col_target = _axis_bounds(columns, width)
    row_source, row_target = _axis_bounds(rows, height)
    return {
        'columns': columns, 'rows': rows,
        'col_source': col_source, 'col_target': col_target,
        'row_source': row_source, 'row_target': row_target,
    }


def _anchor_point(anchor, rect):
    x, y, w, h = rect
    left, center_x, right = x, x + w // 2, x + w
    top, center_y, bottom = y, y + h // 2, y + h
    table = {
        'top_left': (left, top), 'top': (center_x, top), 'top_right': (right, top),
        'left': (left, center_y), 'center': (center_x, center_y), 'right': (right, center_y),
        'bottom_left': (left, bottom), 'bottom': (center_x, bottom), 'bottom_right': (right, bottom),
    }
    return table[anchor]


def solve_layout(workspace, width, height):
    """Compute target rectangles for grid cells and decorations.

    Raises Invalid with concrete conflict messages when minimum gaps or
    centering constraints cannot be satisfied; callers keep the previous
    feasible result instead of mutating anything.
    """
    mapping = grid_mapping(workspace, width, height)
    panel_rect = [0, 0, width, height]
    conflicts = []
    placed = {}
    decorations = workspace.get('decorations', [])
    by_id = {item['id']: item for item in decorations}

    def place(decoration):
        if decoration['id'] in placed:
            return placed[decoration['id']]
        attachment = decoration['attachment']
        w, h = decoration['rect'][2], decoration['rect'][3]
        if attachment['target'] == 'edge':
            host = panel_rect
        else:
            host = place(by_id[attachment['target']])
        anchor_x, anchor_y = _anchor_point(attachment['anchor'], host)
        if attachment['target'] == 'edge':
            # Edge anchors encode the intended side; the anchor point sits on
            # the panel border and the decoration is placed inward from it.
            if attachment['anchor'] in ('top_left', 'left', 'bottom_left'):
                base_x = anchor_x
            elif attachment['anchor'] in ('top_right', 'right', 'bottom_right'):
                base_x = anchor_x - w
            else:
                base_x = anchor_x - w // 2
            if attachment['anchor'] in ('top_left', 'top', 'top_right'):
                base_y = anchor_y
            elif attachment['anchor'] in ('bottom_left', 'bottom', 'bottom_right'):
                base_y = anchor_y - h
            else:
                base_y = anchor_y - h // 2
        else:
            base_x = anchor_x + (0 if attachment['x_align'] == 'start' else -w if attachment['x_align'] == 'end' else -w // 2)
            base_y = anchor_y + (0 if attachment['y_align'] == 'start' else -h if attachment['y_align'] == 'end' else -h // 2)
        rect_x = base_x + attachment['offset_x']
        rect_y = base_y + attachment['offset_y']
        rect = [rect_x, rect_y, w, h]

        if decoration['center_x']:
            rect[0] = (width - w) // 2
        if decoration['center_y']:
            rect[1] = (height - h) // 2

        gap_l, gap_t, gap_r, gap_b = decoration['min_gap']
        violations = []
        if rect[0] < gap_l:
            violations.append('左侧最小间距 %d，当前可用 %d' % (gap_l, rect[0]))
        if rect[1] < gap_t:
            violations.append('上方最小间距 %d，当前可用 %d' % (gap_t, rect[1]))
        if width - (rect[0] + w) < gap_r:
            violations.append('右侧最小间距 %d，当前可用 %d' % (gap_r, width - rect[0] - w))
        if height - (rect[1] + h) < gap_b:
            violations.append('下方最小间距 %d，当前可用 %d' % (gap_b, height - rect[1] - h))
        if decoration['center_x'] and (rect[0] < gap_l or width - rect[0] - w < gap_r):
            violations.append('横向居中与左右最小间距冲突，无法同时满足')
        if decoration['center_y'] and (rect[1] < gap_t or height - rect[1] - h < gap_b):
            violations.append('纵向居中与上下最小间距冲突，无法同时满足')
        if violations:
            conflicts.append('装饰「%s」：%s' % (decoration['name'], '；'.join(violations)))
        placed[decoration['id']] = rect
        return rect

    for decoration in decorations:
        place(decoration)
    if conflicts:
        raise v.Invalid('面板尺寸 %d×%d 存在约束冲突，已保留上一次可行结果：%s'
                        % (width, height, ' / '.join(conflicts)))
    return {'mapping': mapping, 'decorations': placed, 'width': width, 'height': height}


def describe(workspace, width, height):
    """Non-raising feasibility description used by the preview/dry-run API."""
    try:
        layout = solve_layout(workspace, width, height)
        return {'feasible': True, 'conflicts': [], 'layout': layout}
    except v.Invalid as error:
        return {'feasible': False, 'conflicts': [str(error)], 'layout': None}
