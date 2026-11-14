import hashlib
import json
import random
import re
import time
from collections import deque
from . import validate as v


EDGES = ('north', 'east', 'south', 'west')
OPPOSITE = (2, 3, 0, 1)
DELTAS = ((0, -1), (1, 0), (0, 1), (-1, 0))
WILDCARD = '*'
DIRECTION_LABELS = {'north': '上', 'east': '右', 'south': '下', 'west': '左'}
GID_MASK = 0x1FFFFFFF
FLIP_HORIZONTAL = 0x80000000
FLIP_VERTICAL = 0x40000000
MAX_SOLVE_CELLS = 4096
MAX_PINS = 64
MAX_STEPS = 300000
MAX_CANDIDATES_IN_REPORT = 16
MAX_CONNECTIONS_IN_REPORT = 12000
MAX_FIXED_IN_REPORT = 200
EXPLAIN_SECONDS = 12.0


class SolveBudget(Exception):
    pass


def rule_index(value, total):
    if isinstance(value, bool):
        raise v.Invalid('规则瓦片编号无效')
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch('[0-9]{1,4}', value):
        number = int(value)
    elif isinstance(value, str) and re.fullmatch('瓦片[0-9]{1,4}', value):
        number = int(value[2:])
    else:
        raise v.Invalid('规则瓦片编号无效')
    return v.integer(number, '规则瓦片编号', 1, total)


def parse_edge_rules(value, tile_count):
    data = v.object_value(value, '边缘规则')
    name = v.text(data.get('name', '地形边缘规则'), '规则集名称', 60, False)
    tiles = v.object_value(data.get('tiles', {}), '规则瓦片')
    if not tiles:
        raise v.Invalid('请至少为一个瓦片配置边缘标签')
    if len(tiles) > tile_count:
        raise v.Invalid('规则瓦片数量超出瓦片集合')
    entries = {}
    for raw_key, raw_edges in tiles.items():
        index = rule_index(raw_key, tile_count)
        label = f'瓦片{index}'
        if label in entries:
            raise v.Invalid('规则瓦片编号重复')
        raw_edges = v.object_value(raw_edges, label + '边缘')
        unknown = set(raw_edges) - set(EDGES)
        if unknown:
            raise v.Invalid('存在未知边缘方向：' + '、'.join(sorted(unknown)))
        entry = {}
        for direction in EDGES:
            entry[direction] = v.text(raw_edges.get(direction, WILDCARD), label + '边缘标签', 32, False)
        entries[label] = entry
    if not any(token != WILDCARD for entry in entries.values() for token in entry.values()):
        raise v.Invalid('边缘规则至少需要一个非通配标签')
    return {'name': name, 'tiles': entries}


def validate_stored_rules(tilemap):
    if 'edge_rules' in tilemap:
        parse_edge_rules(tilemap['edge_rules'], len(tilemap['tiles']))
    if 'rule_pins' in tilemap:
        pins = tilemap['rule_pins']
        if not isinstance(pins, list) or len(pins) > MAX_PINS:
            raise v.Invalid('保存的固定格无效')
        seen = set()
        for item in pins:
            v.object_value(item, '保存的固定格')
            x = v.integer(item.get('x'), '固定格X', 0, tilemap['columns'] - 1)
            y = v.integer(item.get('y'), '固定格Y', 0, tilemap['rows'] - 1)
            if (x, y) in seen:
                raise v.Invalid('保存的固定格坐标重复')
            seen.add((x, y))
            v.integer(item.get('tile'), '固定瓦片编号', 1, len(tilemap['tiles']))
            v.boolean(item.get('flip_x', False), '固定格水平翻转')
            v.boolean(item.get('flip_y', False), '固定格垂直翻转')


def rules_for(tilemap):
    if 'edge_rules' not in tilemap:
        return None
    return parse_edge_rules(tilemap['edge_rules'], len(tilemap['tiles']))


def effective_edges(rule_tiles, tile, flip_x=False, flip_y=False):
    base = rule_tiles.get(f'瓦片{tile}')
    if base is None:
        return {direction: WILDCARD for direction in EDGES}
    result = dict(base)
    if flip_x:
        result['east'], result['west'] = result['west'], result['east']
    if flip_y:
        result['north'], result['south'] = result['south'], result['north']
    return result


def pair_supported(first, second, direction):
    first_token = first[EDGES[direction]]
    second_token = second[EDGES[OPPOSITE[direction]]]
    return first_token == second_token or first_token == WILDCARD or second_token == WILDCARD


def parse_region(value, columns, rows):
    value = v.object_value(value, '铺设区域')
    x = v.integer(value.get('x', 0), '区域X', 0, columns - 1)
    y = v.integer(value.get('y', 0), '区域Y', 0, rows - 1)
    width = v.integer(value.get('width', columns - x), '区域宽度', 1, columns - x)
    height = v.integer(value.get('height', rows - y), '区域高度', 1, rows - y)
    return x, y, width, height


def layer_for(tilemap, layer_id):
    if layer_id is None:
        return tilemap['layers'][0]
    v.key(layer_id)
    layer = next((item for item in tilemap['layers'] if item['id'] == layer_id), None)
    if layer is None:
        raise v.Invalid('地图图层不存在')
    return layer


def base_value(raw):
    return raw & GID_MASK


def flags_of(value):
    return bool(value & FLIP_HORIZONTAL), bool(value & FLIP_VERTICAL)


def parse_pins(raw, tilemap, layer, region):
    x0, y0, width, height = region
    pins = []
    seen = set()
    for item in v.array(raw, '固定格', MAX_PINS):
        if isinstance(item, dict):
            coords = [item.get('x'), item.get('y')]
            tile = item.get('tile')
            flip_x = item.get('flip_x', False)
            flip_y = item.get('flip_y', False)
        elif isinstance(item, (list, tuple)):
            if not 2 <= len(item) <= 5:
                raise v.Invalid('固定格参数格式无效')
            coords = list(item[:2])
            tile = item[2] if len(item) >= 3 else None
            flip_x = item[3] if len(item) >= 4 else False
            flip_y = item[4] if len(item) >= 5 else False
        else:
            raise v.Invalid('固定格参数格式无效')
        if len(coords) != 2:
            raise v.Invalid('固定格需要两个坐标分量')
        x = v.integer(coords[0], '固定格X', 0, tilemap['columns'] - 1)
        y = v.integer(coords[1], '固定格Y', 0, tilemap['rows'] - 1)
        if (x, y) in seen:
            raise v.Invalid('固定格坐标不能重复')
        seen.add((x, y))
        raw_value = layer['data'][y * tilemap['columns'] + x]
        inside = x0 <= x < x0 + width and y0 <= y < y0 + height
        if tile is None:
            base = base_value(raw_value)
            if not base:
                raise v.Invalid('该固定格为空，请明确指定瓦片编号')
            flip_x, flip_y = flags_of(raw_value)
        else:
            base = v.integer(tile, '固定瓦片编号', 1, len(tilemap['tiles']))
            flip_x = v.boolean(flip_x, '固定格水平翻转')
            flip_y = v.boolean(flip_y, '固定格垂直翻转')
            if not inside and base_value(raw_value) != base:
                if not base_value(raw_value):
                    raise v.Invalid(f'区域外固定格 ({x},{y}) 在该地图图层为空；区域外固定格必须与已放置瓦片一致，或改用区域内固定格')
                raise v.Invalid(f'区域外固定格 ({x},{y}) 与已放置瓦片不一致，区域外必须保持不变')
        pins.append({'x': x, 'y': y, 'tile': base, 'flip_x': flip_x, 'flip_y': flip_y, 'inside': inside})
    return pins


def cell_info(coord, value, source):
    return {'x': coord[0], 'y': coord[1], 'tile': base_value(value), 'flip_x': flags_of(value)[0],
            'flip_y': flags_of(value)[1], 'source': source}


def pin_info(pin):
    return {'x': pin['x'], 'y': pin['y'], 'tile': pin['tile'], 'flip_x': pin.get('flip_x', False),
            'flip_y': pin.get('flip_y', False), 'source': pin.get('source', '手动固定')}


def rule_info(rules, tile):
    entry = rules['tiles'].get(f'瓦片{tile}')
    return {'tile': tile, 'edges': entry} if entry else None


class RuleSolver:
    def __init__(self, tilemap, layer, region, rules, pins, pin_existing, seed, disabled=frozenset()):
        self.tilemap = tilemap
        self.layer = layer
        self.region = region
        self.rules = rules
        self.seed = seed
        self.pin_existing = pin_existing
        self.disabled = set(disabled)
        self.columns = tilemap['columns']
        self.rows = tilemap['rows']
        self.configured = sorted(int(key[2:]) for key in rules['tiles'])
        self.base_edges = {tile: effective_edges(rules['tiles'], tile) for tile in self.configured}
        x0, y0, width, height = region
        self.cells = [(x0 + dx, y0 + dy) for dy in range(height) for dx in range(width)]
        self.var_index = {coord: index for index, coord in enumerate(self.cells)}
        self.pins = pins
        self.inside_pins = {}
        self.external = {}
        self.fixed_cells = []
        self.steps = 0
        self.decided = set()
        self.failure = None
        self.open_cells = set()
        self._prepare_constraints()

    def region_contains(self, coord):
        x0, y0, width, height = self.region
        return x0 <= coord[0] < x0 + width and y0 <= coord[1] < y0 + height

    def existing(self, coord):
        return self.layer['data'][coord[1] * self.columns + coord[0]]

    def _prepare_constraints(self):
        for pin in self.pins:
            coord = (pin['x'], pin['y'])
            if coord in self.disabled:
                continue
            if pin['inside']:
                self.inside_pins[coord] = pin
                self.fixed_cells.append(dict(pin, source='手动固定'))
            else:
                self.external[coord] = {'pin': dict(pin, source='区域外固定')}
                self.fixed_cells.append(dict(pin, source='区域外固定'))
        if self.pin_existing:
            for coord in self.cells:
                if coord in self.inside_pins or coord in self.disabled:
                    continue
                raw = self.existing(coord)
                if base_value(raw):
                    pin = {'x': coord[0], 'y': coord[1], 'tile': base_value(raw),
                           'flip_x': flags_of(raw)[0], 'flip_y': flags_of(raw)[1], 'inside': True}
                    self.inside_pins[coord] = pin
                    self.fixed_cells.append(dict(pin, source='已放置固定'))
        x0, y0, width, height = self.region
        for coord in self.cells:
            x, y = coord
            for dx, dy in DELTAS:
                other = (x + dx, y + dy)
                if not (0 <= other[0] < self.columns and 0 <= other[1] < self.rows):
                    continue
                if x0 <= other[0] < x0 + width and y0 <= other[1] < y0 + height:
                    continue
                if other in self.disabled:
                    continue
                if other in self.external:
                    continue
                raw = self.existing(other)
                if not base_value(raw):
                    continue
                info = cell_info(other, raw, '区域外边界')
                self.external[other] = {'border': info}
                self.fixed_cells.append(dict(info))

    def edges_for(self, coord, tile):
        pin = self.inside_pins.get(coord)
        if pin and pin['tile'] == tile:
            return effective_edges(self.rules['tiles'], pin['tile'], pin['flip_x'], pin['flip_y'])
        return self.base_edges.get(tile, {direction: WILDCARD for direction in EDGES})

    def external_edges(self, info):
        return effective_edges(self.rules['tiles'], info['tile'], info.get('flip_x', False), info.get('flip_y', False))

    def external_fixes(self, coord):
        x, y = coord
        result = []
        for direction, (dx, dy) in enumerate(DELTAS):
            item = self.external.get((x + dx, y + dy))
            if not item:
                continue
            available = item.get('pin') or item.get('border')
            if available is None:
                continue
            result.append((direction, available))
        return result

    def build_domains(self):
        domains = {coord: set(self.configured) for coord in self.cells}
        for coord, pin in self.inside_pins.items():
            domains[coord] = {pin['tile']}
        for coord in self.cells:
            fixes = self.external_fixes(coord)
            if not fixes:
                continue
            allowed = set()
            for tile in domains[coord]:
                if all(pair_supported(self.edges_for(coord, tile), self.external_edges(info), direction)
                       for direction, info in fixes):
                    allowed.add(tile)
            domains[coord] &= allowed
        return domains

    def region_neighbor(self, coord, direction):
        dx, dy = DELTAS[direction]
        other = (coord[0] + dx, coord[1] + dy)
        return other if self.region_contains(other) else None

    def all_arcs(self):
        for coord in self.cells:
            for direction in range(4):
                if self.region_neighbor(coord, direction) is not None:
                    yield coord, direction

    def arcs_for_change(self, coord):
        result = []
        for direction in range(4):
            neighbor = self.region_neighbor(coord, direction)
            if neighbor is not None:
                result.append((neighbor, OPPOSITE[direction]))
        return result

    def _propagate_queue(self, domains, queue):
        while queue:
            coord, direction = queue.popleft()
            self.steps += 1
            if self.steps > MAX_STEPS:
                raise SolveBudget()
            neighbor = self.region_neighbor(coord, direction)
            domain = domains[coord]
            neighbor_domain = domains[neighbor]
            removed = []
            for tile in list(domain):
                first = self.edges_for(coord, tile)
                if not any(pair_supported(first, self.edges_for(neighbor, other), direction) for other in neighbor_domain):
                    domain.discard(tile)
                    removed.append(tile)
            if not removed:
                continue
            if not domain:
                neighbor_edges = {str(tile): effective_edges(self.rules['tiles'], tile) for tile in sorted(neighbor_domain)}
                self.failure = {
                    'cell': [coord[0], coord[1]],
                    'direction': EDGES[direction],
                    'neighbor': [neighbor[0], neighbor[1]],
                    'neighbor_candidates': sorted(neighbor_domain)[:MAX_CANDIDATES_IN_REPORT],
                    'removed_tiles': sorted(removed)[:MAX_CANDIDATES_IN_REPORT],
                    'neighbor_edges': neighbor_edges if len(neighbor_edges) <= MAX_CANDIDATES_IN_REPORT else {},
                    'required_token': None,
                }
                return False
            if len(domain) == 1:
                self.open_cells.discard(coord)
            for arc in self.arcs_for_change(coord):
                queue.append(arc)
        return True

    def choose(self, domains):
        best = None
        for coord in self.open_cells:
            size = len(domains[coord])
            if size <= 1:
                continue
            key = (size, self.var_index[coord])
            if best is None or key < best[0]:
                best = (key, coord)
        return best[1] if best else None

    def candidates(self, coord, domain):
        rng = random.Random((self.seed * 1000003 + self.var_index[coord] * 7919) & 0x7FFFFFFF)
        options = sorted(domain)
        rng.shuffle(options)
        return options

    def search(self, domains):
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise SolveBudget()
        coord = self.choose(domains)
        if coord is None:
            return domains
        saved_open = set(self.open_cells)
        saved_decided = set(self.decided)
        for tile in self.candidates(coord, domains[coord]):
            trial = {item: set(values) for item, values in domains.items()}
            trial[coord] = {tile}
            self.open_cells = set(saved_open)
            self.open_cells.discard(coord)
            if self._propagate_from(trial, coord):
                self.decided.add(coord)
                found = self.search(trial)
                if found is not None:
                    return found
                self.decided.discard(coord)
        self.open_cells = saved_open
        self.decided = saved_decided
        return None

    def _propagate_from(self, domains, coord):
        queue = deque(self.arcs_for_change(coord))
        return self._propagate_queue(domains, queue)

    def solve(self):
        domains = self.build_domains()
        for coord, domain in domains.items():
            if not domain:
                self.failure = {'cell': [coord[0], coord[1]], 'direction': None, 'neighbor': None,
                                'neighbor_candidates': [], 'removed_tiles': []}
                return None
        self.open_cells = {coord for coord, domain in domains.items() if len(domain) > 1}
        if not self._propagate_queue(domains, deque(self.all_arcs())):
            return None
        if not self.open_cells:
            return domains
        return self.search(domains)

    def assignment(self, coord, tile):
        pin = self.inside_pins.get(coord)
        if pin:
            return pin['tile'] | (FLIP_HORIZONTAL if pin['flip_x'] else 0) | (FLIP_VERTICAL if pin['flip_y'] else 0), 'pinned'
        return tile, ('solution' if coord in self.decided else 'forced')

    def assigned_cells(self, domains):
        result = []
        for coord in self.cells:
            domain = domains[coord]
            if len(domain) != 1:
                continue
            tile = next(iter(domain))
            raw, status = self.assignment(coord, tile)
            result.append({'x': coord[0], 'y': coord[1], 'tile': tile, 'value': raw, 'status': status})
        return result

    def undetermined_cells(self, domains):
        result = []
        for coord in self.cells:
            domain = domains[coord]
            if len(domain) <= 1:
                continue
            candidates = sorted(domain)
            result.append({
                'x': coord[0], 'y': coord[1],
                'candidate_count': len(candidates),
                'candidates': candidates[:MAX_CANDIDATES_IN_REPORT],
                'candidates_truncated': len(candidates) > MAX_CANDIDATES_IN_REPORT,
                'fixed': bool(self.inside_pins.get(coord)),
            })
        return result

    def connections(self, assigned):
        values = {(item['x'], item['y']): item for item in assigned}
        fixed = {}
        for info in self.fixed_cells:
            fixed[(info['x'], info['y'])] = info
        result = []
        by_token = {}
        for coord, item in values.items():
            x, y = coord
            for direction, label in ((1, 'east'), (2, 'south'), (3, 'west'), (0, 'north')):
                dx, dy = DELTAS[direction]
                other = (x + dx, y + dy)
                if other in values:
                    if direction not in (1, 2):
                        continue
                    first = self.edges_for(coord, item['tile'])
                    second = self.edges_for(other, values[other]['tile'])
                    second_tile = values[other]['tile']
                    to_border = False
                elif other in fixed:
                    first = self.edges_for(coord, item['tile'])
                    second = self.external_edges(fixed[other])
                    second_tile = fixed[other]['tile']
                    to_border = True
                else:
                    continue
                if not pair_supported(first, second, direction):
                    continue
                a_token, b_token = first[EDGES[direction]], second[EDGES[OPPOSITE[direction]]]
                token = b_token if a_token == WILDCARD else a_token
                entry = {
                    'first': [x, y], 'second': [other[0], other[1]], 'direction': label,
                    'tokens': [a_token, b_token], 'token': token,
                    'tiles': [item['tile'], second_tile], 'to_border': to_border,
                }
                result.append(entry)
                by_token[token] = by_token.get(token, 0) + 1
        return result, by_token


def fixed_pair_conflicts(solver, rules):
    pairs = []
    cells = solver.fixed_cells
    for index, first in enumerate(cells):
        for second in cells[index + 1:]:
            xy = (first['x'], first['y'])
            other = (second['x'], second['y'])
            for direction in (1, 2):
                dx, dy = DELTAS[direction]
                if (xy[0] + dx, xy[1] + dy) == other:
                    a, b, d = first, second, direction
                elif (other[0] + dx, other[1] + dy) == xy:
                    a, b, d = second, first, OPPOSITE[direction]
                else:
                    continue
                first_edges = effective_edges(rules['tiles'], a['tile'], a.get('flip_x', False), a.get('flip_y', False))
                second_edges = effective_edges(rules['tiles'], b['tile'], b.get('flip_x', False), b.get('flip_y', False))
                if pair_supported(first_edges, second_edges, d):
                    continue
                pairs.append({
                    'kind': 'fixed_pair',
                    'message': f'固定格 ({a["x"]},{a["y"]})瓦片{a["tile"]} 的{DIRECTION_LABELS[EDGES[d]]}边缘“{first_edges[EDGES[d]]}”'
                               f'与固定格 ({b["x"]},{b["y"]})瓦片{b["tile"]} 的对应边缘'
                               f'“{second_edges[EDGES[OPPOSITE[d]]]}”不匹配。',
                    'cells': [[a['x'], a['y']], [b['x'], b['y']]],
                    'fixed': [pin_info(a), pin_info(b)],
                    'edges': [{'first': [a['x'], a['y']], 'second': [b['x'], b['y']],
                               'direction': EDGES[d],
                               'tokens': [first_edges[EDGES[d]], second_edges[EDGES[OPPOSITE[d]]]]}],
                    'rules': [item for item in [rule_info(rules, a['tile']), rule_info(rules, b['tile'])] if item],
                })
    return pairs


def collect_fixed(solver):
    result = [dict(info) for info in solver.fixed_cells]
    order = {'手动固定': 0, '区域外固定': 1, '区域外边界': 2, '已放置固定': 3}
    result.sort(key=lambda info: (order.get(info.get('source'), 9), info['y'], info['x']))
    return result[:MAX_FIXED_IN_REPORT]


def solve_trial(tilemap, layer, region, rules, pins, pin_existing, seed, disabled):
    solver = RuleSolver(tilemap, layer, region, rules, pins, pin_existing, seed, disabled)
    try:
        return solver, solver.solve()
    except SolveBudget:
        return solver, 'budget'


def minimize_fixed(tilemap, layer, region, rules, pins, pin_existing, seed, solver):
    fixed = collect_fixed(solver)
    disabled = set()
    deadline = time.monotonic() + EXPLAIN_SECONDS
    for info in fixed:
        if time.monotonic() > deadline:
            break
        coord = (info['x'], info['y'])
        if coord in disabled:
            continue
        trial = disabled | {coord}
        _, outcome = solve_trial(tilemap, layer, region, rules, pins, pin_existing, seed, trial)
        if outcome == 'budget':
            continue
        if outcome is None:
            disabled.add(coord)
    return fixed, disabled


def conflicting_of(fixed, disabled):
    return [info for info in fixed if (info['x'], info['y']) not in disabled]


def _base_result(tilemap, layer, rules, region, mode, seed, pins, solver, assigned, undetermined, connections, by_token):
    x0, y0, width, height = region
    counts = {'pinned': 0, 'forced': 0, 'solution': 0}
    for item in assigned:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    fixed_border = [info for info in solver.fixed_cells if not solver.region_contains((info['x'], info['y']))]
    return {
        'status': 'solved',
        'map_id': tilemap['id'],
        'map_name': tilemap['name'],
        'layer_id': layer['id'],
        'layer_name': layer['name'],
        'rules': {'name': rules['name'], 'defined_tiles': len(rules['tiles']),
                  'candidate_tiles': solver.configured},
        'region': {'x': x0, 'y': y0, 'width': width, 'height': height},
        'mode': mode,
        'seed': seed,
        'pin_existing': solver.pin_existing,
        'pins': [pin_info(pin) for pin in pins],
        'fixed_border': fixed_border[:MAX_FIXED_IN_REPORT],
        'cells': assigned,
        'undetermined': undetermined,
        'connections': connections[:MAX_CONNECTIONS_IN_REPORT],
        'connection_count': len(connections),
        'connection_tokens': by_token,
        'stats': {
            'region_cells': len(solver.cells),
            'assigned': len(assigned),
            'undetermined': len(undetermined),
            'pinned': counts['pinned'],
            'forced': counts['forced'],
            'solution': counts['solution'],
            'search_steps': solver.steps,
        },
    }


def _fingerprint(project, map_id, layer_id, region, mode, seed, pin_existing, pins, rules, cells):
    payload = {
        'revision': project['revision'], 'map_id': map_id, 'layer_id': layer_id,
        'region': list(region), 'mode': mode, 'seed': seed, 'pin_existing': pin_existing,
        'pins': [{'x': p['x'], 'y': p['y'], 'tile': p['tile'], 'flip_x': p['flip_x'], 'flip_y': p['flip_y']} for p in pins],
        'cells': [{'x': c['x'], 'y': c['y'], 'value': c['value'], 'status': c['status']} for c in cells],
        'rules': rules,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf8')).hexdigest()


def _conflict_result(project, tilemap, layer, rules, region, mode, seed, pins, pin_existing, solver):
    fixed, disabled = minimize_fixed(tilemap, layer, region, rules, pins, pin_existing, seed, solver)
    core = conflicting_of(fixed, disabled)
    pairs = fixed_pair_conflicts(solver, rules)
    dead_end = solver.failure or {}
    conflicts = [dict(pair) for pair in pairs]
    core_pins = [info for info in core if info.get('source') in ('手动固定', '已放置固定')]
    core_border = [info for info in core if info.get('source') in ('区域外边界', '区域外固定')]
    if core_pins:
        message = '固定格组合在规则集“' + rules['name'] + '”下无解：'
        message += '、'.join(f'({info["x"]},{info["y"]})瓦片{info["tile"]}' for info in core_pins) + '。'
        kind = 'pin_rule'
    elif core_border:
        message = '区域外已放置瓦片与规则集“' + rules['name'] + '”冲突：'
        message += '、'.join(f'({info["x"]},{info["y"]})瓦片{info["tile"]}' for info in core_border) + '。'
        kind = 'border_rule'
    else:
        message = '所选区域在规则集“' + rules['name'] + '”下无解，与固定格无关，请调整区域或规则。'
        kind = 'rule'
    dead_cell = dead_end.get('cell')
    if dead_cell:
        message += f' 规则推导在 ({dead_cell[0]},{dead_cell[1]}) 处已无候选瓦片。'
    dead_cells = []
    for coord in ([dead_cell] if dead_cell else []) + [[info['x'], info['y']] for info in core]:
        if coord not in dead_cells:
            dead_cells.append(coord)
    conflicts.append({
        'kind': kind,
        'message': message,
        'cells': dead_cells,
        'fixed': core,
        'pins': core_pins,
        'edges': [],
        'rules': [item for item in [rule_info(rules, info['tile']) for info in core] if item],
    })
    implicated = []
    seen = set()
    for info in core:
        entry = rule_info(rules, info['tile'])
        if entry and entry['tile'] not in seen:
            seen.add(entry['tile'])
            implicated.append(entry)
    if not implicated and dead_end.get('removed_tiles') and dead_end.get('cell'):
        pos = dead_end['cell']
        left_rules = sorted({tile for tile in dead_end.get('removed_tiles', [])})
        right_rules = sorted({tile for tile in dead_end.get('neighbor_candidates', [])})
        for tile in left_rules + right_rules:
            entry = rule_info(rules, tile)
            if entry and entry['tile'] not in seen:
                seen.add(entry['tile'])
                implicated.append(entry)
        if implicated:
            conflicts.append({
                'kind': 'rule_edge',
                'message': f'推导在 ({pos[0]},{pos[1]}) 的{DIRECTION_LABELS.get(dead_end["direction"], dead_end["direction"])}边缘无可行候选，涉及瓦片 '
                           + '、'.join(str(item['tile']) for item in implicated) + ' 的边缘定义。',
                'cells': [pos, dead_end.get('neighbor')],
                'fixed': [],
                'pins': [],
                'edges': [{'first': pos, 'second': dead_end.get('neighbor'), 'direction': dead_end['direction'],
                           'tokens': [None, None]}],
                'rules': implicated,
            })
    if not implicated:
        for tile in solver.configured[:MAX_CANDIDATES_IN_REPORT]:
            entry = rule_info(rules, tile)
            if entry:
                implicated.append(entry)
    result = _base_result(tilemap, layer, rules, region, mode, seed, pins, solver, [], [], [], {})
    result.update({
        'status': 'conflict',
        'conflicts': conflicts,
        'conflicting_pins': core_pins,
        'conflicting_fixed': core,
        'conflicting_rules': [{'name': rules['name'], **entry} for entry in implicated],
        'dead_end': dead_end,
        'dead_end_cells': dead_cells,
        'message': message,
        'fingerprint': None,
    })
    return result


def preview(project, map_id, body):
    from .tilemaps import find_map
    tilemap = find_map(project, map_id)
    rules = rules_for(tilemap)
    if rules is None:
        raise v.Invalid('请先为该地图配置瓦片边缘连接规则')
    layer = layer_for(tilemap, body.get('layer_id'))
    region = parse_region(body.get('region', {}), tilemap['columns'], tilemap['rows'])
    if region[2] * region[3] > MAX_SOLVE_CELLS:
        raise v.Invalid(f'规则铺设区域最多{MAX_SOLVE_CELLS}格，请缩小区域')
    mode = v.choice(body.get('mode', 'solve'), '铺设模式', ['solve', 'propagate'])
    seed = v.integer(body.get('seed', 7), '随机种子', 0, 2147483647)
    pin_existing = v.boolean(body.get('pin_existing', False), '固定已放置格')
    raw_pins = body.get('pins')
    if raw_pins is None:
        raw_pins = tilemap.get('rule_pins', [])
    pins = parse_pins(raw_pins, tilemap, layer, region)
    solver = RuleSolver(tilemap, layer, region, rules, pins, pin_existing, seed)
    if not solver.configured:
        raise v.Invalid('请先为该地图配置至少一个边缘规则瓦片')

    def finish(result):
        result['fingerprint'] = _fingerprint(project, map_id, layer['id'], region, mode, seed,
                                             pin_existing, pins, rules, result['cells'])
        return result

    if mode == 'propagate':
        domains = solver.build_domains()
        if any(not domain for domain in domains.values()):
            for coord, domain in domains.items():
                if not domain:
                    solver.failure = {'cell': [coord[0], coord[1]], 'direction': None, 'neighbor': None,
                                      'neighbor_candidates': [], 'removed_tiles': []}
                    break
            return _conflict_result(project, tilemap, layer, rules, region, mode, seed, pins, pin_existing, solver)
        solver.open_cells = {coord for coord, domain in domains.items() if len(domain) > 1}
        if not solver._propagate_queue(domains, deque(solver.all_arcs())):
            return _conflict_result(project, tilemap, layer, rules, region, mode, seed, pins, pin_existing, solver)
        assigned = solver.assigned_cells(domains)
        undetermined = solver.undetermined_cells(domains)
        connections, by_token = solver.connections(assigned)
        result = _base_result(tilemap, layer, rules, region, mode, seed, pins, solver, assigned, undetermined,
                              connections, by_token)
        return finish(result)
    try:
        domains = solver.solve()
    except SolveBudget:
        raise v.Invalid('规则搜索步数超限，请缩小区域、增加固定格或更换种子')
    if domains is None:
        return _conflict_result(project, tilemap, layer, rules, region, mode, seed, pins, pin_existing, solver)
    assigned = solver.assigned_cells(domains)
    if len(assigned) != len(solver.cells):
        raise v.Invalid('规则求解结果不完整')
    connections, by_token = solver.connections(assigned)
    result = _base_result(tilemap, layer, rules, region, mode, seed, pins, solver, assigned, [],
                          connections, by_token)
    return finish(result)


def rules_summary(project, map_id):
    from .tilemaps import find_map
    tilemap = find_map(project, map_id)
    rules = rules_for(tilemap)
    return {
        'map_id': tilemap['id'], 'map_name': tilemap['name'],
        'columns': tilemap['columns'], 'rows': tilemap['rows'],
        'tile_count': len(tilemap['tiles']),
        'tiles': [{'index': index + 1, 'frame_id': frame_id} for index, frame_id in enumerate(tilemap['tiles'])],
        'rules': rules,
        'configured': rules is not None,
        'saved_pins': list(tilemap.get('rule_pins', [])),
        'layers': [{'id': layer['id'], 'name': layer['name'], 'visible': layer['visible']} for layer in tilemap['layers']],
    }


def set_rules(store, project_id, revision, map_id, body):
    from .tilemaps import find_map
    with store.edit(project_id, revision, '设置瓦片边缘规则') as project:
        tilemap = find_map(project, map_id)
        if body.get('rules') is None:
            tilemap.pop('edge_rules', None)
        else:
            tilemap['edge_rules'] = parse_edge_rules(body.get('rules'), len(tilemap['tiles']))
    return store.get(project_id)


def verify_assignments(tilemap, layer, rules, region, assignments):
    x0, y0, width, height = region
    values = {(item['x'], item['y']): item for item in assignments}
    external = {}
    for coord in values:
        for dx, dy in DELTAS:
            other = (coord[0] + dx, coord[1] + dy)
            if other in values or other in external:
                continue
            if not (0 <= other[0] < tilemap['columns'] and 0 <= other[1] < tilemap['rows']):
                continue
            if x0 <= other[0] < x0 + width and y0 <= other[1] < y0 + height:
                continue
            raw = layer['data'][other[1] * tilemap['columns'] + other[0]]
            if base_value(raw):
                external[other] = cell_info(other, raw, '区域外边界')
    for coord, item in values.items():
        first_flip = flags_of(item['value'])
        first = effective_edges(rules['tiles'], item['tile'], *first_flip)
        for direction in range(4):
            dx, dy = DELTAS[direction]
            other = (coord[0] + dx, coord[1] + dy)
            if not (0 <= other[0] < tilemap['columns'] and 0 <= other[1] < tilemap['rows']):
                continue
            if other in values:
                second_info = values[other]
                second = effective_edges(rules['tiles'], second_info['tile'], *flags_of(second_info['value']))
            elif other in external:
                second_info = external[other]
                second = effective_edges(rules['tiles'], second_info['tile'], second_info['flip_x'], second_info['flip_y'])
            else:
                continue
            if not pair_supported(first, second, direction):
                return [{
                    'message': f'({coord[0]},{coord[1]}) 与 ({other[0]},{other[1]}) 的边缘标签不匹配。',
                    'cells': [[coord[0], coord[1]], [other[0], other[1]]],
                    'tokens': [first[EDGES[direction]], second[EDGES[OPPOSITE[direction]]]],
                }]
    return []


def commit(store, project_id, revision, map_id, body):
    from .tilemaps import find_map
    body = v.object_value(body)
    mode = body.get('mode', 'solve')
    if mode != 'solve':
        raise v.Invalid('只有完整铺设结果可以确认；仅推导模式仍有未确定格')
    with store.lock:
        project = store.get(project_id)
        if revision != project['revision']:
            raise v.Conflict('项目已修改，请重新求解预览后再确认')
    rules = rules_for(find_map(project, map_id))
    if rules is None:
        raise v.Invalid('请先为该地图配置瓦片边缘连接规则')
    expected = body.get('fingerprint')
    if expected is not None:
        expected = v.text(expected, '预览指纹', 128, False)
    with store.lock:
        result = preview(project, map_id, body)
        if result['status'] != 'solved':
            raise v.Invalid(result.get('message', '规则铺设无解') + ' 未修改地图。')
        if expected is not None and result.get('fingerprint') != expected:
            raise v.Conflict('地图或规则已变化，请重新求解预览后再确认')
        region = result['region']
        with store.edit(project_id, revision, '确认规则铺设') as working:
            tilemap = find_map(working, map_id)
            layer = layer_for(tilemap, result['layer_id'])
            area = (region['x'], region['y'], region['width'], region['height'])
            assignments = result['cells']
            if len(assignments) != region['width'] * region['height']:
                raise v.Invalid('铺设结果不完整，未修改地图')
            verify = verify_assignments(tilemap, layer, rules, area, assignments)
            if verify:
                raise v.Invalid('铺设结果与边缘规则不一致，未修改地图：' + verify[0]['message'])
            saved = [{'x': item['x'], 'y': item['y'], 'tile': item['tile'],
                      'flip_x': item.get('flip_x', False), 'flip_y': item.get('flip_y', False)}
                     for item in result['pins']][:MAX_PINS]
            if saved:
                tilemap['rule_pins'] = saved
            else:
                tilemap.pop('rule_pins', None)
            changed = []
            for item in assignments:
                index = item['y'] * tilemap['columns'] + item['x']
                if layer['data'][index] != item['value']:
                    layer['data'][index] = item['value']
                    changed.append(index)
            offsets = layer.get('phase_offsets')
            if offsets:
                for index in changed:
                    offsets.pop(str(index), None)
    return store.get(project_id)
