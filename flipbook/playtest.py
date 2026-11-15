import hashlib
import json
from collections import deque
from . import validate as v
from .model import new_id
from .tilemaps import find_map


DIRECTIONS = {'up': (0, -1), 'down': (0, 1), 'left': (-1, 0), 'right': (1, 0)}
DIRECTION_NAMES = {'up': '上', 'down': '下', 'left': '左', 'right': '右'}
KEY_KINDS = ['gold', 'silver', 'red', 'blue', 'green', 'purple']
KIND_NAMES = {'gold': '金', 'silver': '银', 'red': '红', 'blue': '蓝', 'green': '绿', 'purple': '紫'}
LIMITS = {'keys': 16, 'doors': 16, 'switches': 10, 'gates': 64, 'oneways': 500, 'blocks': 5000, 'moves': 2000}
DEFAULT_STATE_BUDGET = 200000
MAX_STATE_BUDGET = 1000000
REPORT_LIMIT = 400
INCOMPLETE_NOTE = '状态空间超出承受范围，分析尚未完成，不能据此断定无解'


def empty_config():
    return {'start': None, 'exit': None, 'blocks': [], 'keys': [], 'doors': [], 'switches': [], 'gates': [], 'oneways': []}


def cell(value, columns, rows, name='格子坐标'):
    values = v.array(value, name, 2)
    if len(values) != 2:
        raise v.Invalid(name + '需要x和y')
    return [v.integer(values[0], '格子X', 0, columns - 1), v.integer(values[1], '格子Y', 0, rows - 1)]


def item_id(raw):
    if raw is None:
        return new_id()
    return v.key(raw)


def normalize_config(tilemap, body):
    columns, rows = tilemap['columns'], tilemap['rows']
    body = v.object_value(body if body is not None else {}, '机关设置')
    config = empty_config()
    for name in ('start', 'exit'):
        raw = body.get(name)
        config[name] = None if raw is None else cell(raw, columns, rows, '起终点坐标')
    if config['start'] and config['exit'] and config['start'] == config['exit']:
        raise v.Invalid('起点和出口不能在同一格')
    blocked = set()
    for raw in v.array(body.get('blocks', []), '阻挡格', LIMITS['blocks']):
        point = cell(raw, columns, rows, '阻挡格坐标')
        blocked.add(tuple(point))
    config['blocks'] = [list(point) for point in sorted(blocked)]

    def device_list(field, label, limit, extra):
        items = []
        for raw in v.array(body.get(field, []), label, limit):
            item = v.object_value(raw, label)
            entry = {'id': item_id(item.get('id')), 'at': cell(item.get('at'), columns, rows, label + '坐标')}
            extra(entry, item, items)
            items.append(entry)
        return items

    config['keys'] = device_list('keys', '钥匙', LIMITS['keys'], lambda entry, raw, items: entry.update(kind=v.choice(raw.get('kind'), '钥匙种类', KEY_KINDS)))
    config['doors'] = device_list('doors', '门', LIMITS['doors'], lambda entry, raw, items: entry.update(kind=v.choice(raw.get('kind'), '门钥匙种类', KEY_KINDS)))

    def switch_extra(entry, raw, items):
        entry['name'] = v.text(raw.get('name', '开关' + str(len(items) + 1)), '开关名称', 30, False)
        entry['on'] = v.boolean(raw.get('on', False), '开关初始状态')

    config['switches'] = device_list('switches', '开关', LIMITS['switches'], switch_extra)
    switch_ids = {item['id'] for item in config['switches']}
    if len(switch_ids) != len(config['switches']):
        raise v.Invalid('开关编号重复')

    def gate_extra(entry, raw, items):
        linked = raw.get('switch')
        if linked not in switch_ids:
            raise v.Invalid('闸门引用的开关不存在')
        entry['switch'] = linked

    config['gates'] = device_list('gates', '闸门', LIMITS['gates'], gate_extra)
    seen_ids = set()
    for name in ('keys', 'doors', 'switches', 'gates'):
        for item in config[name]:
            if item['id'] in seen_ids:
                raise v.Invalid('机关编号重复')
            seen_ids.add(item['id'])
    usage = {}
    for name, label in (('keys', '钥匙'), ('doors', '门'), ('switches', '开关'), ('gates', '闸门')):
        for item in config[name]:
            point = tuple(item['at'])
            if point in blocked:
                raise v.Invalid(label + '不能放在阻挡格')
            if point in usage:
                raise v.Invalid('同一格不能同时放置' + usage[point] + '和' + label)
            usage[point] = label
    oneway_cells = set()
    for raw in v.array(body.get('oneways', []), '单向通道', LIMITS['oneways']):
        item = v.object_value(raw, '单向通道')
        point = tuple(cell(item.get('at'), columns, rows, '单向通道坐标'))
        if point in blocked:
            raise v.Invalid('单向通道不能放在阻挡格')
        if point in oneway_cells:
            raise v.Invalid('同一格只能有一个单向标记')
        oneway_cells.add(point)
        config['oneways'].append({'at': list(point), 'dir': v.choice(item.get('dir'), '单向方向', list(DIRECTIONS))})
    for name, label in (('start', '起点'), ('exit', '出口')):
        point = config[name]
        if point is not None and tuple(point) in blocked:
            raise v.Invalid(label + '不能在阻挡格')
    if config['start'] is not None and tuple(config['start']) in usage:
        raise v.Invalid('起点格不能放钥匙、门、开关或闸门')
    if config['exit'] is not None and usage.get(tuple(config['exit'])) in ('钥匙', '开关'):
        raise v.Invalid('出口格不能放钥匙或开关')
    return config


def validate_playtest(tilemap):
    stored = tilemap.get('playtest')
    if stored is None:
        return
    if normalize_config(tilemap, stored) != stored:
        raise v.Invalid('通关试验机关设置结构不完整')


def fingerprint(tilemap, config):
    payload = json.dumps({'columns': tilemap['columns'], 'rows': tilemap['rows'], 'config': config}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode('utf8')).hexdigest()[:16]


class Env:
    def __init__(self, tilemap, config):
        self.columns = tilemap['columns']
        self.rows = tilemap['rows']
        self.blocked = {y * self.columns + x for x, y in config['blocks']}
        self.oneway = {}
        for item in config['oneways']:
            x, y = item['at']
            self.oneway[y * self.columns + x] = item['dir']
        self.keys = config['keys']
        self.doors = config['doors']
        self.switches = config['switches']
        self.key_at = self._index_by_cell(self.keys)
        self.door_at = self._index_by_cell(self.doors)
        self.switch_at = self._index_by_cell(self.switches)
        switch_index = {item['id']: i for i, item in enumerate(self.switches)}
        self.gate_at = {}
        for gate in config['gates']:
            x, y = gate['at']
            self.gate_at[y * self.columns + x] = switch_index[gate['switch']]
        self.start = self._cell_index(config['start'])
        self.exit = self._cell_index(config['exit'])
        self.initial_switches = sum(1 << i for i, item in enumerate(self.switches) if item['on'])

    def _cell_index(self, point):
        if point is None:
            return None
        return point[1] * self.columns + point[0]

    def _index_by_cell(self, items):
        return {item['at'][1] * self.columns + item['at'][0]: i for i, item in enumerate(items)}

    def initial_state(self):
        return (self.start, 0, 0, self.initial_switches)


def inventory_count(env, keys_mask, doors_mask, kind):
    count = 0
    for i, item in enumerate(env.keys):
        if item['kind'] == kind and (keys_mask >> i) & 1:
            count += 1
    for j, item in enumerate(env.doors):
        if item['kind'] == kind and (doors_mask >> j) & 1:
            count -= 1
    return count


def step(env, state, direction):
    pos, keys_mask, doors_mask, switch_mask = state
    if pos == env.exit:
        return None, '已经到达出口'
    here = env.oneway.get(pos)
    if here and here != direction:
        return None, '单向通道只能向' + DIRECTION_NAMES[here] + '离开'
    dx, dy = DIRECTIONS[direction]
    x, y = pos % env.columns, pos // env.columns
    nx, ny = x + dx, y + dy
    if not (0 <= nx < env.columns and 0 <= ny < env.rows):
        return None, '地图边界外'
    npos = ny * env.columns + nx
    there = env.oneway.get(npos)
    if there and there != direction:
        return None, '单向通道只能沿' + DIRECTION_NAMES[there] + '方向进入'
    if npos in env.blocked:
        return None, '被阻挡格挡住'
    gate = env.gate_at.get(npos)
    if gate is not None and not (switch_mask >> gate) & 1:
        return None, '闸门关闭，需要先拨动对应开关'
    events = []
    door = env.door_at.get(npos)
    if door is not None and not (doors_mask >> door) & 1:
        kind = env.doors[door]['kind']
        if inventory_count(env, keys_mask, doors_mask, kind) <= 0:
            return None, '门已锁，需要' + KIND_NAMES[kind] + '钥匙'
        doors_mask |= 1 << door
        events.append('用' + KIND_NAMES[kind] + '钥匙开门，钥匙已消耗')
    key = env.key_at.get(npos)
    if key is not None and not (keys_mask >> key) & 1:
        keys_mask |= 1 << key
        events.append('拾取' + KIND_NAMES[env.keys[key]['kind']] + '钥匙')
    switch = env.switch_at.get(npos)
    if switch is not None:
        switch_mask ^= 1 << switch
        events.append('拨动' + env.switches[switch]['name'] + '：' + ('开' if (switch_mask >> switch) & 1 else '关'))
    return (npos, keys_mask, doors_mask, switch_mask), events


def public_state(env, state):
    pos, keys_mask, doors_mask, switch_mask = state
    inventory = {}
    for i, item in enumerate(env.keys):
        if (keys_mask >> i) & 1:
            inventory[item['kind']] = inventory.get(item['kind'], 0) + 1
    for j, item in enumerate(env.doors):
        if (doors_mask >> j) & 1:
            inventory[item['kind']] = inventory.get(item['kind'], 0) - 1
    return {
        'pos': [pos % env.columns, pos // env.columns],
        'inventory': {kind: count for kind, count in inventory.items() if count > 0},
        'collected_keys': [env.keys[i]['id'] for i in range(len(env.keys)) if (keys_mask >> i) & 1],
        'open_doors': [env.doors[j]['id'] for j in range(len(env.doors)) if (doors_mask >> j) & 1],
        'switches': {env.switches[i]['id']: bool((switch_mask >> i) & 1) for i in range(len(env.switches))},
        'won': pos == env.exit,
    }


def simulate(env, moves):
    state = env.initial_state()
    raw = [state]
    events = []
    applied = 0
    rejected = None
    for i, direction in enumerate(moves):
        nxt, info = step(env, state, direction)
        if nxt is None:
            rejected = {'index': i, 'direction': direction, 'reason': info}
            break
        state = nxt
        applied += 1
        if state[0] == env.exit:
            info = info + ['到达出口，通关！']
        events.append(info)
        raw.append(state)
    return raw, events, applied, rejected


def explore(env, start_state, budget, stop_at_goal=False):
    states = [start_state]
    ids = {start_state: 0}
    parent = [(-1, '')]
    pred = [[]]
    goals = []
    complete = True
    queue = deque([0])
    while queue:
        sid = queue.popleft()
        state = states[sid]
        if state[0] == env.exit:
            goals.append(sid)
            if stop_at_goal:
                return True, states, ids, parent, pred, goals
            continue
        for direction in DIRECTIONS:
            nxt, _ = step(env, state, direction)
            if nxt is None:
                continue
            nid = ids.get(nxt)
            if nid is None:
                if len(states) >= budget:
                    complete = False
                    continue
                nid = len(states)
                states.append(nxt)
                ids[nxt] = nid
                parent.append((sid, direction))
                pred.append([])
                queue.append(nid)
            pred[nid].append(sid)
    return complete, states, ids, parent, pred, goals


def route_to(parent, goal):
    moves = []
    sid = goal
    while parent[sid][0] != -1:
        sid, move = parent[sid][0], parent[sid][1]
        moves.append(move)
    moves.reverse()
    return moves


def route_positions(env, start_state, moves):
    positions = [[start_state[0] % env.columns, start_state[0] // env.columns]]
    state = start_state
    for direction in moves:
        state, _ = step(env, state, direction)
        positions.append([state[0] % env.columns, state[0] // env.columns])
    return positions


def route_from(env, start_state, budget):
    if start_state[0] == env.exit:
        return {'status': 'already_won', 'moves': [], 'positions': route_positions(env, start_state, []), 'steps': 0, 'explored': 0}
    complete, states, ids, parent, pred, goals = explore(env, start_state, budget, stop_at_goal=True)
    if goals:
        moves = route_to(parent, goals[0])
        return {'status': 'found', 'moves': moves, 'positions': route_positions(env, start_state, moves), 'steps': len(moves), 'explored': len(states)}
    result = {'status': 'impossible' if complete else 'unknown', 'moves': None, 'positions': None, 'explored': len(states)}
    if not complete:
        result['note'] = INCOMPLETE_NOTE
    return result


def analyze_level(env, budget):
    complete, states, ids, parent, pred, goals = explore(env, env.initial_state(), budget)
    result = {'complete': complete, 'explored': len(states)}
    if not complete:
        result.update({'solvable': None, 'reachable_states': None, 'unreachable_cells': None, 'doomed_cells': None,
                       'softlock_states': None, 'softlock_transitions': None, 'softlock_transition_count': None, 'note': INCOMPLETE_NOTE})
        return result
    good = set(goals)
    queue = deque(goals)
    while queue:
        sid = queue.popleft()
        for pid in pred[sid]:
            if pid not in good:
                good.add(pid)
                queue.append(pid)
    reachable = {state[0] for state in states}
    unreachable = [[i % env.columns, i // env.columns] for i in range(env.columns * env.rows)
                   if i not in env.blocked and i not in reachable]
    doomed_cells = sorted({states[sid][0] for sid in range(len(states)) if sid not in good})
    transitions = {}
    for sid in range(len(states)):
        if sid not in good:
            continue
        state = states[sid]
        for direction in DIRECTIONS:
            nxt, _ = step(env, state, direction)
            if nxt is None:
                continue
            if ids[nxt] not in good:
                key = (state[0], direction)
                transitions[key] = {'from': [state[0] % env.columns, state[0] // env.columns], 'direction': direction,
                                    'to': [nxt[0] % env.columns, nxt[0] // env.columns]}
    ordered = [transitions[key] for key in sorted(transitions)]
    result.update({
        'solvable': bool(goals),
        'reachable_states': len(states),
        'unreachable_cells': unreachable,
        'doomed_cells': [[i % env.columns, i // env.columns] for i in doomed_cells],
        'softlock_states': len(states) - len(good),
        'softlock_transitions': ordered[:REPORT_LIMIT],
        'softlock_transition_count': len(ordered),
    })
    return result


def playtest_env(project, map_id):
    tilemap = find_map(project, map_id)
    config = tilemap.get('playtest') or empty_config()
    if not config['start'] or not config['exit']:
        raise v.Invalid('请先在地图上标记起点和出口')
    return tilemap, config, Env(tilemap, config)


def parse_moves(body):
    return [v.choice(item, '移动方向', list(DIRECTIONS)) for item in v.array(body.get('moves', []), '移动序列', LIMITS['moves'])]


def save_config(store, project_id, revision, map_id, body):
    with store.edit(project_id, revision, '修改通关试验机关') as project:
        tilemap = find_map(project, map_id)
        config = normalize_config(tilemap, body.get('config', body))
        if config == empty_config():
            tilemap.pop('playtest', None)
        else:
            tilemap['playtest'] = config
    return store.get(project_id)


def simulate_moves(project, map_id, body):
    tilemap, config, env = playtest_env(project, map_id)
    raw, events, applied, rejected = simulate(env, parse_moves(body))
    return {
        'fingerprint': fingerprint(tilemap, config),
        'states': [public_state(env, state) for state in raw],
        'events': events,
        'applied': applied,
        'rejected': rejected,
        'won': raw[-1][0] == env.exit,
    }


def analyze_moves(project, map_id, body):
    tilemap, config, env = playtest_env(project, map_id)
    budget = v.integer(body.get('max_states', DEFAULT_STATE_BUDGET), '状态上限', 1000, MAX_STATE_BUDGET)
    raw, events, applied, rejected = simulate(env, parse_moves(body))
    current = raw[-1]
    return {
        'fingerprint': fingerprint(tilemap, config),
        'applied': applied,
        'rejected': rejected,
        'current': public_state(env, current),
        'route': route_from(env, current, budget),
        'level': analyze_level(env, budget),
        'max_states': budget,
    }
