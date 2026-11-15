import io
import json
import zipfile
import numpy as np
from PIL import Image, ImageDraw
from . import validate as v
from .composite import render_frame, png_bytes

MAX_POSITIONS = 8192
MAX_TOOL_PIXELS = 8000000
MAX_BANK_COUNT = 32


def hex_of(rgb):
    return '#%02x%02x%02x' % tuple(int(value) for value in rgb)


def sq_distances(pixels, colors):
    rgb = pixels.astype(np.int32)
    palette = np.asarray(colors, dtype=np.int32)
    if not len(palette):
        raise v.Invalid('色库没有可用颜色')
    deltas = rgb[:, None, :] - palette[None, :, :]
    return np.sum(deltas * deltas, axis=2)


def nearest_mapping(pixels, colors):
    distance = sq_distances(pixels, colors)
    indices = np.argmin(distance, axis=1)
    losses = distance[np.arange(len(distance)), indices]
    return indices, losses


class TilePalettes:
    def __init__(self, store):
        self.store = store

    def _parameters(self, project, body):
        body = v.object_value(body, '参数')
        ids = v.frame_ids(body.get('frame_ids') or [frame['id'] for frame in project['frames']], project)
        ordered = [frame['id'] for frame in project['frames'] if frame['id'] in set(ids)]
        bank_count = v.integer(body.get('bank_count', 8), '色库数量', 1, MAX_BANK_COUNT)
        slots = v.integer(body.get('colors_per_tile', 4), '每块颜色名额', 2, 16)
        tile_size = v.integer(body.get('tile_size', 8), '小块尺寸', 4, 32)
        threshold = v.integer(body.get('alpha_threshold', 128), '透明阈值', 0, 255)
        pinned = list(dict.fromkeys(v.hex_color(item) for item in v.array(body.get('pinned_colors', []), '固定颜色', 64)))
        if len(pinned) > slots:
            raise v.Invalid(f'固定颜色 {len(pinned)} 种，已超过每块 {slots} 个名额')
        cols = int(np.ceil(project['width'] / tile_size))
        rows = int(np.ceil(project['height'] / tile_size))
        positions = cols * rows
        if positions > MAX_POSITIONS:
            raise v.Invalid(f'小块数量 {positions} 超过 {MAX_POSITIONS}，请增大小块尺寸或缩小画布')
        protected = self._protected(body.get('protected_tiles', []), cols, rows, tile_size, set(ids))
        return {
            'frame_ids': ordered, 'bank_count': bank_count, 'slots': slots,
            'tile_size': tile_size, 'threshold': threshold, 'pinned': pinned,
            'cols': cols, 'rows': rows, 'protected': protected,
        }

    def _protected(self, raw, cols, rows, tile_size, known_frames):
        result = {}
        for entry in v.array(raw, '保护小块', MAX_POSITIONS):
            entry = v.object_value(entry, '保护小块')
            if 'gx' in entry or 'gy' in entry:
                gx = v.integer(entry.get('gx'), '小块列号', 0, cols - 1)
                gy = v.integer(entry.get('gy'), '小块列号', 0, rows - 1)
            else:
                x = v.integer(entry.get('x'), '像素X', 0)
                y = v.integer(entry.get('y'), '像素Y', 0)
                gx, gy = x // tile_size, y // tile_size
                if not (0 <= gx < cols and 0 <= gy < rows):
                    raise v.Invalid('保护小块坐标超出画布')
            frames = entry.get('frames')
            if frames is None:
                frame_set = {None}
            else:
                frame_set = set(v.array(frames, '保护帧编号', 300))
                unknown = [item for item in frame_set if item not in known_frames]
                if unknown:
                    raise v.Invalid('保护小块引用了不存在的帧')
            result.setdefault((gx, gy), set()).update(frame_set)
        return result

    def _frames(self, project, ids, tile_size):
        arrays = []
        for frame_id in ids:
            image = render_frame(self.store, project, frame_id)
            arrays.append(np.asarray(image.convert('RGBA'), dtype=np.uint8))
        height, width = arrays[0].shape[:2]
        total = height * width * len(arrays)
        if total > MAX_TOOL_PIXELS:
            raise v.Invalid(f'分块减色限 {MAX_TOOL_PIXELS} 像素，请减少帧数或画布尺寸')
        cols = int(np.ceil(width / tile_size))
        rows = int(np.ceil(height / tile_size))
        padded = np.zeros((len(arrays), rows * tile_size, cols * tile_size, 4), dtype=np.uint8)
        for index, array in enumerate(arrays):
            padded[index, :height, :width] = array
        blocks = padded.reshape(len(arrays), rows, tile_size, cols, tile_size, 4)
        blocks = blocks.transpose(1, 3, 0, 2, 4, 5).reshape(rows * cols, len(arrays) * tile_size * tile_size, 4)
        return arrays, blocks, cols, rows, width, height

    def _summarize_positions(self, params, blocks, frame_count):
        threshold = params['threshold']
        ts = params['tile_size']
        positions = []
        for gy in range(params['rows']):
            for gx in range(params['cols']):
                block = blocks[gy * params['cols'] + gx]
                alpha = block[:, 3]
                visible = alpha >= threshold
                transparent = int(np.count_nonzero(~visible))
                rgb = block[visible][:, :3]
                counts = {}
                if len(rgb):
                    values, frequency = np.unique(rgb, axis=0, return_counts=True)
                    for color, count in zip(values, frequency):
                        counts[tuple(int(c) for c in color)] = int(count)
                ordered_colors = sorted(counts, key=lambda color: (-counts[color], color))
                protected_frames = params['protected'].get((gx, gy))
                protected = protected_frames is not None
                required = set(ordered_colors) if protected and None in protected_frames else set()
                if protected and None not in protected_frames:
                    for frame_id in protected_frames:
                        frame_index = params['frame_ids'].index(frame_id)
                        tile = blocks[gy * params['cols'] + gx].reshape(frame_count, ts * ts, 4)[frame_index]
                        tile_rgb = tile[tile[:, 3] >= threshold][:, :3]
                        if len(tile_rgb):
                            required.update(tuple(int(c) for c in color) for color in np.unique(tile_rgb, axis=0))
                positions.append({
                    'gx': gx, 'gy': gy, 'counts': counts, 'colors': ordered_colors,
                    'transparent': transparent, 'has_transparent': transparent > 0,
                    'protected': protected, 'required': required,
                    'visible_pixels': int(len(rgb)),
                })
        return positions

    def _visible_capacity(self, position, slots):
        return slots - 1 if position['has_transparent'] else slots

    def _candidate(self, bank, position, slots):
        colors = list(bank)
        if position['protected']:
            additions = [color for color in position['colors'] if color in position['required'] and color not in colors]
            if len(colors) + len(additions) > slots or len(position['required']) > self._visible_capacity(position, slots):
                return None
            colors.extend(additions)
            return colors
        for color in position['colors']:
            if len(colors) >= slots:
                break
            if color not in colors:
                colors.append(color)
        if not colors:
            return None
        return colors

    def _position_loss(self, position, blocks, index, bank_colors, threshold):
        block = blocks[index]
        rgb = block[block[:, 3] >= threshold][:, :3]
        if not len(rgb):
            return 0
        if not len(bank_colors):
            return float(3 * 255 * 255 * len(rgb))
        return float(nearest_mapping(rgb, bank_colors)[1].sum())

    def _assign(self, params, positions, blocks):
        slots = params['slots']
        threshold = params['threshold']
        pinned = [v.rgba(color)[:3] for color in params['pinned']]
        banks = [list(pinned) for _ in range(params['bank_count'])]
        assignment = [-1] * len(positions)
        conflicts = []

        def order_key(position):
            protection = 0 if position['protected'] else 1
            return (protection, -len(position['required'] or position['colors']),
                    -position['visible_pixels'], position['gy'], position['gx'])

        ordered_indexes = sorted(range(len(positions)), key=lambda i: order_key(positions[i]))
        for index in ordered_indexes:
            position = positions[index]
            capacity = self._visible_capacity(position, slots)
            if position['protected'] and len(position['required']) > capacity:
                assignment[index] = 0
                conflicts.append({
                    'gx': position['gx'], 'gy': position['gy'],
                    'reason': f'保护小块需要 {len(position["required"])} 种可见颜色，超过该块 {capacity} 个可见名额（透明已占 1 名额）',
                    'needed_visible': len(position['required']), 'capacity_visible': capacity,
                })
                continue
            best = None
            for bank_index, bank in enumerate(banks):
                candidate = self._candidate(bank, position, slots)
                if candidate is None:
                    continue
                loss = self._position_loss(position, blocks, index, candidate, threshold)
                additions = sum(1 for color in candidate if color not in bank)
                key = (loss, additions, bank_index)
                if best is None or key < best[0]:
                    best = (key, bank_index, candidate)
            if best is None:
                non_empty = [bank_index for bank_index, bank in enumerate(banks) if bank]
                fallback = min(
                    non_empty or range(len(banks)),
                    key=lambda bank_index: (self._position_loss(position, blocks, index, banks[bank_index] or pinned, threshold), bank_index),
                )
                assignment[index] = fallback
                conflicts.append({
                    'gx': position['gx'], 'gy': position['gy'],
                    'reason': f'{len(banks)} 个色库均已满，无法再容纳该小块颜色；请增加色库数量或每块名额',
                    'needed_visible': len(position['required'] or position['colors']), 'capacity_visible': capacity,
                })
                continue
            _, bank_index, candidate = best
            banks[bank_index] = candidate
            assignment[index] = bank_index

        for _ in range(2):
            moved = False
            for index in ordered_indexes:
                position = positions[index]
                current = assignment[index]
                current_loss = self._position_loss(position, blocks, index, banks[current], threshold)
                best = (current_loss, current)
                for bank_index, bank in enumerate(banks):
                    if bank_index == current:
                        continue
                    candidate = self._candidate(bank, position, slots)
                    if candidate is None:
                        continue
                    loss = self._position_loss(position, blocks, index, candidate, threshold)
                    key = (loss, sum(1 for color in candidate if color not in bank), bank_index)
                    if key < best:
                        best = (loss, bank_index)
                if best[1] != current:
                    target = best[1]
                    rebuilt = self._candidate(banks[target], position, slots)
                    banks[target] = rebuilt
                    banks[current] = self._rebuild_bank(params, positions, blocks, assignment, current, exclude=index)
                    assignment[index] = target
                    moved = True
            if not moved:
                break
        return banks, assignment, conflicts

    def _rebuild_bank(self, params, positions, blocks, assignment, bank_index, exclude=-1):
        slots = params['slots']
        pinned = [v.rgba(color)[:3] for color in params['pinned']]
        members = [i for i, owner in enumerate(assignment) if owner == bank_index and i != exclude]
        totals = {}
        for index in members:
            for color, count in positions[index]['counts'].items():
                totals[color] = totals.get(color, 0) + count
        colors = list(pinned)
        protected_required = []
        for index in members:
            position = positions[index]
            if position['protected']:
                protected_required.extend(sorted(position['required']))
        for color in protected_required:
            if color not in colors:
                colors.append(color)
        for color in sorted(totals, key=lambda item: (-totals[item], item)):
            if len(colors) >= slots:
                break
            if color not in colors:
                colors.append(color)
        return colors[:slots]

    def plan(self, project_id, body):
        project = self.store.get(project_id)
        params = self._parameters(project, body)
        arrays, blocks, cols, rows, width, height = self._frames(project, params['frame_ids'], params['tile_size'])
        frame_count = len(arrays)
        positions = self._summarize_positions(params, blocks, frame_count)
        banks, assignment, conflicts = self._assign(params, positions, blocks)
        slot_usage = [0] * len(banks)
        quantized, index_maps, tile_records = self._encode(
            params, project, arrays, blocks, positions, banks, assignment, slot_usage,
        )
        total_loss = sum(record['loss'] for record in tile_records)
        visible_pixels = sum(position['visible_pixels'] for position in positions)
        position_reports = []
        for index, position in enumerate(positions):
            bank_index = assignment[index]
            tiles = [record for record in tile_records if record['position'] == index]
            position_reports.append({
                'gx': position['gx'], 'gy': position['gy'],
                'grid': [position['gx'], position['gy']],
                'bank': bank_index,
                'protected': position['protected'],
                'has_transparent': position['has_transparent'],
                'visible_capacity': self._visible_capacity(position, params['slots']),
                'distinct_visible_colors': len(position['colors']),
                'transparent_pixels': position['transparent'],
                'visible_pixels': position['visible_pixels'],
                'loss': round(sum(item['loss'] for item in tiles), 4),
                'tiles': tiles,
            })
        bank_reports = []
        for bank_index, colors in enumerate(banks):
            bank_reports.append({
                'index': bank_index,
                'colors': [hex_of(color) for color in colors],
                'pinned': params['pinned'],
                'slots': params['slots'],
                'used_slots': len(colors),
                'positions': [[p['gx'], p['gy']] for i, p in enumerate(positions) if assignment[i] == bank_index],
            })
        frame_lookup = {frame['id']: frame for frame in project['frames']}
        frame_meta = [
            {'frame_id': frame_id, 'frame_index': index,
             'name': frame_lookup[frame_id]['name'], 'duration': frame_lookup[frame_id]['duration']}
            for index, frame_id in enumerate(params['frame_ids'])
        ]
        return {
            'feasible': not conflicts,
            'parameters': {
                'bank_count': params['bank_count'], 'colors_per_tile': params['slots'],
                'tile_size': params['tile_size'], 'alpha_threshold': params['threshold'],
                'pinned_colors': params['pinned'],
                'protected_tiles': [{'gx': gx, 'gy': gy} for (gx, gy) in sorted(params['protected'])],
                'frame_ids': params['frame_ids'],
            },
            'canvas': {'width': width, 'height': height, 'columns': cols, 'rows': rows},
            'frames': frame_meta,
            'banks': bank_reports,
            'positions': position_reports,
            'conflicts': conflicts,
            'totals': {
                'positions': len(positions),
                'tile_instances': len(tile_records),
                'banks_used': len([bank for bank in banks if bank]),
                'banks_total': len(banks),
                'visible_pixels': visible_pixels,
                'transparent_pixels': sum(position['transparent'] for position in positions),
                'protected_positions': sum(1 for position in positions if position['protected']),
                'total_squared_loss': round(total_loss, 4),
                'mean_squared_error': round(total_loss / max(visible_pixels, 1), 4),
            },
        }, quantized, index_maps, params

    def _encode(self, params, project, arrays, blocks, positions, banks, assignment, slot_usage):
        ts = params['tile_size']
        threshold = params['threshold']
        quantized = [np.zeros_like(array) for array in arrays]
        index_maps = [np.zeros(array.shape[:2], dtype=np.uint8) for array in arrays]
        records = []
        for index, position in enumerate(positions):
            bank_index = assignment[index]
            bank_colors = banks[bank_index]
            gx, gy = position['gx'], position['gy']
            x0, y0 = gx * ts, gy * ts
            x1, y1 = min(x0 + ts, project['width']), min(y0 + ts, project['height'])
            for frame_index, frame_id in enumerate(params['frame_ids']):
                tile = arrays[frame_index][y0:y1, x0:x1]
                alpha = tile[:, :, 3] >= threshold
                rgb = tile[alpha][:, :3]
                local_colors = []
                loss = 0.0
                if len(rgb):
                    unique_tile = sorted(
                        {tuple(int(c) for c in color) for color in np.unique(rgb, axis=0)},
                        key=lambda color: (-int(np.count_nonzero(np.all(rgb == color, axis=1))), color),
                    )
                    chosen = []
                    for color in unique_tile:
                        if position['protected'] and color in position['required']:
                            if color not in chosen and color in bank_colors:
                                chosen.append(color)
                    for color in unique_tile:
                        if len(chosen) >= self._visible_capacity(position, params['slots']):
                            break
                        if color not in chosen:
                            chosen.append(color)
                    if not chosen:
                        chosen = [bank_colors[0]] if bank_colors else unique_tile[:1]
                    chosen = chosen[:self._visible_capacity(position, params['slots'])]
                    local_colors = [hex_of(color) for color in chosen]
                    indices, losses = nearest_mapping(rgb, chosen)
                    loss = float(losses.sum())
                    mapped = np.asarray(chosen, dtype=np.uint8)[indices]
                    output_tile = quantized[frame_index][y0:y1, x0:x1]
                    index_tile = index_maps[frame_index][y0:y1, x0:x1]
                    output_tile[alpha] = np.concatenate([mapped, np.full((len(mapped), 1), 255, dtype=np.uint8)], axis=1)
                    if position['has_transparent']:
                        index_tile[alpha] = indices + 1
                        index_tile[~alpha] = 0
                    else:
                        index_tile[alpha] = indices
                records.append({
                    'position': index, 'frame_id': frame_id, 'frame_index': frame_index,
                    'gx': gx, 'gy': gy, 'bank': bank_index,
                    'transparent_slot': position['has_transparent'],
                    'transparent_pixels': int(np.count_nonzero(~alpha)),
                    'distinct_visible': int(len(np.unique(rgb, axis=0))) if len(rgb) else 0,
                    'local_colors': local_colors,
                    'loss': round(loss, 4),
                })
            slot_usage[bank_index] = max(slot_usage[bank_index], len(bank_colors))
        return quantized, index_maps, records

    def preview(self, project_id, body):
        plan, quantized, _, params = self.plan(project_id, body)
        frame_index = v.integer(body.get('frame_index', 0), '预览帧序号', 0, len(quantized) - 1)
        image = Image.fromarray(quantized[frame_index])
        if body.get('overlay') is True:
            image = self._overlay(image, plan, params, frame_index)
        return image, plan

    def _overlay(self, image, plan, params, frame_index):
        image = image.convert('RGBA')
        ts = params['tile_size']
        draw = ImageDraw.Draw(image, 'RGBA')
        palette = plan['banks']
        for position in plan['positions']:
            x0, y0 = position['gx'] * ts, position['gy'] * ts
            bank = palette[position['bank']]
            tint = v.rgba(bank['colors'][0])[:3] if bank['colors'] else (120, 120, 120)
            draw.rectangle((x0, y0, min(x0 + ts, image.width) - 1, min(y0 + ts, image.height) - 1),
                           fill=(*tint, 70), outline=(255, 255, 255, 110))
            if ts >= 8:
                draw.text((x0 + 1, y0 + 1), str(position['bank']), fill=(255, 255, 255, 230))
            if position['protected']:
                draw.rectangle((x0, y0, min(x0 + ts, image.width) - 1, min(y0 + ts, image.height) - 1),
                               outline=(255, 92, 120, 255))
        return image

    def export(self, project_id, body):
        plan, quantized, index_maps, params = self.plan(project_id, body)
        if plan['conflicts']:
            tiles = '、'.join(f"({item['gx']},{item['gy']})" for item in plan['conflicts'][:8])
            raise v.Conflict('存在放不下保护要求的小块：' + tiles + '；请增加色库/名额或取消对应保护，系统不会暗中多用颜色')
        verification = self._verify(quantized, index_maps, plan, params)
        plan['verification'] = verification
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('tile-palette/tile-palette.json',
                             json.dumps(plan, ensure_ascii=False, allow_nan=False, indent=2).encode('utf8'))
            placeholder = [value for rgb in range(256) for value in (rgb, rgb, rgb)]
            for frame_index, (frame_id, index_map, image_array) in enumerate(
                    zip(params['frame_ids'], index_maps, quantized)):
                indexed = Image.fromarray(index_map, mode='P')
                indexed.putpalette(placeholder)
                buffer = io.BytesIO()
                indexed.save(buffer, format='PNG')
                archive.writestr(f'tile-palette/frames/frame-{frame_index + 1:04d}-{frame_id}.index.png', buffer.getvalue())
                archive.writestr(f'tile-palette/frames/frame-{frame_index + 1:04d}-{frame_id}.png', png_bytes(Image.fromarray(image_array)))
            archive.writestr('tile-palette/banks.png', png_bytes(self._bank_sheet(plan)))
            archive.writestr('tile-palette/README.txt', self._readme(plan).encode('utf8'))
        return stream.getvalue(), plan

    def _bank_sheet(self, plan):
        slots = plan['parameters']['colors_per_tile']
        cell = 24
        sheet = Image.new('RGBA', (slots * cell + 90, len(plan['banks']) * cell + cell), (17, 25, 35, 255))
        draw = ImageDraw.Draw(sheet)
        for bank in plan['banks']:
            y = bank['index'] * cell
            draw.text((4, y + 6), f"库 {bank['index']}", fill=(220, 235, 245, 255))
            for slot in range(slots):
                x = 80 + slot * cell
                if slot < len(bank['colors']):
                    draw.rectangle((x, y, x + cell - 2, y + cell - 2), fill=v.rgba(bank['colors'][slot]))
                draw.rectangle((x, y, x + cell - 2, y + cell - 2), outline=(90, 110, 125, 255))
        return sheet

    def _readme(self, plan):
        parameters = plan['parameters']
        lines = [
            'Flipbook Studio 受限色彩分块色库素材',
            '',
            f"小块尺寸：{parameters['tile_size']}×{parameters['tile_size']}；每块名额：{parameters['colors_per_tile']}（含透明名额）",
            f"色库数量：{plan['totals']['banks_used']}/{plan['totals']['banks_total']}；同一网格位置在全部帧中固定使用同一色库，动作播放不会跳库。",
            '',
            'tile-palette.json：色库颜色、每个小块的色库归属、局部索引映射、逐块减色损失与还原校验结果。',
            'frames/*.index.png：256色调色板占位的索引像素，真实颜色以 JSON 为准。',
            '含透明像素的小块：局部索引 0 固定为透明，1 起对应 local_colors；不透明小块从 0 开始。',
            'frames/*.png：按索引与色库还原出的确认画面（可见像素 Alpha 已按透明阈值二值化）。',
            '固定颜色存在于每个色库；保护小块的原始可见颜色均被完整保留，校验全部通过时才会生成本包。',
        ]
        return '\n'.join(lines) + '\n'

    def _verify(self, quantized, index_maps, plan, params):
        ts = params['tile_size']
        exact = 0
        checked = 0
        for frame_index, frame_id in enumerate(params['frame_ids']):
            rebuilt = np.zeros_like(quantized[frame_index])
            for position in plan['positions']:
                gx, gy = position['gx'], position['gy']
                x0, y0 = gx * ts, gy * ts
                x1, y1 = min(x0 + ts, rebuilt.shape[1]), min(y0 + ts, rebuilt.shape[0])
                index_tile = index_maps[frame_index][y0:y1, x0:x1]
                record = next(item for item in position['tiles'] if item['frame_id'] == frame_id)
                local = [v.rgba(color) for color in record['local_colors']]
                target = rebuilt[y0:y1, x0:x1]
                if not local:
                    continue
                if record['transparent_slot']:
                    visible = index_tile > 0
                    target[visible] = [
                        (*local[value - 1][:3], 255) for value in index_tile[visible]
                    ]
                else:
                    target[:] = np.asarray(
                        [(*local[value][:3], 255) for value in index_tile.reshape(-1)], dtype=np.uint8
                    ).reshape(target.shape)
            checked += 1
            if np.array_equal(rebuilt, quantized[frame_index]):
                exact += 1
        if exact != checked:
            raise v.Invalid('索引像素还原校验失败，已中止导出')
        max_index = params['slots'] - 1
        in_range = all(int(index_map.max(initial=0)) <= max_index for index_map in index_maps)
        return {
            'frames_checked': checked,
            'exact_reconstructions': exact,
            'all_exact': exact == checked,
            'indices_within_slots': bool(in_range),
            'max_local_index_allowed': max_index,
        }
