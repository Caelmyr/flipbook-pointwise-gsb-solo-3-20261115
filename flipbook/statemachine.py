"""Action state machine: clip-backed states, prioritized guarded transitions,
uninterruptible windows, buffered inputs and one-shot fallbacks.

Time is always virtual integer milliseconds; the engine never reads a wall
clock, so a recorded input script replays with identical results.
"""
from copy import deepcopy
from . import validate as v
from .model import find_frame, new_id
from .exposures import clip_exposures

PHASE_PRESS = 'press'
PHASE_RELEASE = 'release'
MAX_MACHINES = 8
MAX_STATES = 24
MAX_TRANSITIONS = 128
MAX_EVENTS = 400
MAX_HISTORY = 200
DEFAULT_BUFFER = 300


def clip_sequence(project, clip_id):
    clip = next((item for item in project['clips'] if item['id'] == clip_id), None)
    if clip is None:
        raise v.Invalid('状态引用的动作片段不存在，请先保存片段或调整状态')
    exposures = clip_exposures(project, clip)
    if clip.get('direction') == 'reverse':
        exposures.reverse()
    elif clip.get('direction') == 'pingpong':
        exposures = exposures + [dict(item) for item in exposures[-2:0:-1]]
    return [{'frame_id': item['frame_id'], 'duration': int(item['duration'])} for item in exposures]


def frame_at(sequence, elapsed):
    total = sum(item['duration'] for item in sequence)
    position = elapsed % total
    for item in sequence:
        if position < item['duration']:
            return item['frame_id']
        position -= item['duration']
    return sequence[-1]['frame_id']


def validate_event(value):
    event = v.object_value(value, '输入定义')
    identifier = v.text(event.get('id'), '输入标识', 24, False)
    if not identifier.replace('_', '').isalnum():
        raise v.Invalid('输入标识只能使用字母、数字和下划线')
    return {'id': identifier, 'name': v.text(event.get('name'), '输入名称', 40, False),
            'key': v.text(event.get('key'), '试演按键', 12, False)}


def validate_state(value):
    state = v.object_value(value, '状态定义')
    v.key(state.get('id'))
    mode = v.choice(state.get('mode', 'loop'), '状态播放方式', ['loop', 'oneshot'])
    result = {'id': state['id'], 'name': v.text(state.get('name'), '状态名称', 80, False),
              'clip_id': v.key(state.get('clip_id')), 'mode': mode,
              'uninterruptible': v.integer(state.get('uninterruptible', 0), '不可打断毫秒', 0, 1000000)}
    fallback = state.get('fallback_state_id')
    if fallback is not None:
        result['fallback_state_id'] = v.key(fallback)
    if mode == 'oneshot' and 'fallback_state_id' not in result:
        raise v.Invalid('一次性状态必须指定结束后的回退状态')
    return result


def validate_transition(value):
    transition = v.object_value(value, '切换条件')
    return {
        'id': v.key(transition.get('id')),
        'from_state_id': v.key(transition.get('from_state_id')),
        'to_state_id': v.key(transition.get('to_state_id')),
        'event_id': v.text(transition.get('event_id'), '切换输入', 24, False),
        'phase': v.choice(transition.get('phase', PHASE_PRESS), '触发时机', [PHASE_PRESS, PHASE_RELEASE]),
        'priority': v.integer(transition.get('priority', 0), '优先级', 0, 100000),
        'buffer_window': v.integer(transition.get('buffer_window', DEFAULT_BUFFER), '输入暂存毫秒', 0, 10000),
    }


def validate_machine(value):
    machine = v.object_value(value, '动作状态机')
    if 'id' in machine:
        v.key(machine['id'])
    events = [validate_event(item) for item in v.array(machine.get('events'), '输入定义', 32)]
    event_ids = [item['id'] for item in events]
    if len(set(event_ids)) != len(event_ids):
        raise v.Invalid('输入标识重复')
    states = [validate_state(item) for item in v.array(machine.get('states'), '状态', MAX_STATES)]
    state_ids = [item['id'] for item in states]
    if not states:
        raise v.Invalid('动作状态机至少需要一个状态')
    if len(set(state_ids)) != len(state_ids):
        raise v.Invalid('状态编号重复')
    transitions = [validate_transition(item) for item in v.array(machine.get('transitions'), '切换条件', MAX_TRANSITIONS)]
    if len({item['id'] for item in transitions}) != len(transitions):
        raise v.Invalid('切换条件编号重复')
    known_states, known_events = set(state_ids), set(event_ids)
    initial = v.key(machine.get('initial_state_id'))
    if initial not in known_states:
        raise v.Invalid('入口状态不存在')
    clip_ids = {state['clip_id'] for state in states}
    for state in states:
        if 'fallback_state_id' in state and state['fallback_state_id'] not in known_states:
            raise v.Invalid('回退状态不存在：' + state['name'])
    for transition in transitions:
        if transition['from_state_id'] not in known_states or transition['to_state_id'] not in known_states:
            raise v.Invalid('切换条件引用了不存在的状态')
        if transition['event_id'] not in known_events:
            raise v.Invalid('切换条件引用了未定义的输入：' + transition['event_id'])
    result = {
        'name': v.text(machine.get('name'), '状态机名称', 100, False),
        'events': events,
        'states': states,
        'transitions': transitions,
        'initial_state_id': initial,
    }
    if 'id' in machine:
        result['id'] = machine['id']
    return result, clip_ids


def validate_document(project):
    machines = project.get('state_machines')
    if machines is None:
        return
    clip_ids = {clip['id'] for clip in project.get('clips', [])}
    for machine in v.array(machines, '动作状态机', MAX_MACHINES):
        _, referenced = validate_machine(machine)
        if not referenced <= clip_ids:
            raise v.Invalid('动作状态机引用了不存在的片段，请先调整状态或恢复片段')


def _schedules(project, machine):
    return {state['id']: clip_sequence(project, state['clip_id']) for state in machine['states']}


def schedules(project, machine):
    """Per-state exposure list with cumulative start/end offsets, for the UI."""
    result = {}
    for state in machine['states']:
        sequence = clip_sequence(project, state['clip_id'])
        rows, elapsed = [], 0
        for item in sequence:
            rows.append({'frame_id': item['frame_id'], 'start': elapsed,
                         'end': elapsed + item['duration'], 'duration': item['duration']})
            elapsed += item['duration']
        result[state['id']] = {'frames': rows, 'total_duration': elapsed, 'mode': state['mode'],
                               'uninterruptible': state['uninterruptible']}
    return result


def initial_runtime(machine):
    return {
        'time_ms': 0,
        'state_id': machine['initial_state_id'],
        'state_elapsed': 0,
        'frame_id': None,
        'buffers': [],
        'held': [],
        'last_event': None,
        'history': [],
    }


def _trace(runtime, code, detail):
    entry = {'time_ms': runtime['time_ms'], 'state_id': runtime['state_id'],
             'state_elapsed': runtime['state_elapsed'], 'code': code}
    entry.update(detail)
    runtime['history'].append(entry)
    del runtime['history'][:-MAX_HISTORY]
    return entry


def _buffer_key(event_id, phase):
    return event_id + '|' + phase


def _store_buffer(runtime, event_id, phase, at):
    key = _buffer_key(event_id, phase)
    runtime['buffers'] = [item for item in runtime['buffers'] if item['key'] != key]
    runtime['buffers'].append({'key': key, 'event_id': event_id, 'phase': phase,
                               'at_ms': at, 'state_id': runtime['state_id']})


def _pop_buffer(runtime, event_id, phase=None):
    runtime['buffers'] = [item for item in runtime['buffers']
                          if not (item['event_id'] == event_id and (phase is None or item['phase'] == phase))]


def _eligible(machine, state, transition, runtime, at):
    if transition['from_state_id'] != state['id']:
        return False, '来源状态不匹配'
    key = _buffer_key(transition['event_id'], transition['phase'])
    buffered = next((item for item in runtime['buffers'] if item['key'] == key), None)
    if buffered is None:
        return False, '没有暂存输入：' + transition['event_id']
    age = at - buffered['at_ms']
    if age < 0:
        return False, '暂存输入来自未来时间'
    if age > transition['buffer_window']:
        return False, f'暂存输入已过期（{age}ms > {transition["buffer_window"]}ms）'
    if runtime['state_elapsed'] < state['uninterruptible']:
        return False, f'不可打断区间内（{runtime["state_elapsed"]}ms < {state["uninterruptible"]}ms）'
    return True, f'命中切换，输入暂存{age}ms，优先级{transition["priority"]}'


def _choose(machine, state, runtime, at, trace_blocked=True):
    candidates, rejected = [], []
    for index, transition in enumerate(machine['transitions']):
        ok, reason = _eligible(machine, state, transition, runtime, at)
        if ok:
            candidates.append((index, transition, reason))
        elif trace_blocked and transition['from_state_id'] == state['id']:
            rejected.append({'transition_id': transition['id'], 'event_id': transition['event_id'],
                             'phase': transition['phase'], 'reason': reason})
    if not candidates:
        return None, rejected
    # Deterministic resolution: highest priority, then the transition's stored order.
    winner_index, transition, reason = min(candidates, key=lambda item: (-item[1]['priority'], item[0]))
    losers = [{'transition_id': item[1]['id'], 'event_id': item[1]['event_id'], 'phase': item[1]['phase'],
               'reason': '同刻多条件命中，优先级较低或排序靠后'} for item in candidates if item[0] != winner_index]
    return (transition, reason), rejected + losers


def _enter(machine, runtime, state_id, code, reason):
    state = next(item for item in machine['states'] if item['id'] == state_id)
    runtime['state_id'] = state_id
    runtime['state_elapsed'] = 0
    sequence = runtime['_schedules'][state_id]
    runtime['frame_id'] = sequence[0]['frame_id'] if sequence else None
    _trace(runtime, code, {'to_state_id': state_id, 'frame_id': runtime['frame_id'], 'reason': reason})


def _check_events(machine, events, time_end):
    known = {item['id'] for item in machine['events']}
    checked = []
    for event in v.array(events, '输入事件', MAX_EVENTS):
        at = v.integer(event.get('at_ms'), '事件时间', 0, time_end)
        event_id = v.text(event.get('event_id'), '输入标识', 24, False)
        phase = v.choice(event.get('phase', PHASE_PRESS), '输入时机', [PHASE_PRESS, PHASE_RELEASE])
        checked.append({'at_ms': at, 'event_id': event_id, 'phase': phase, 'known': event_id in known})
    return sorted(checked, key=lambda item: (item['at_ms'], 0 if item['phase'] == PHASE_PRESS else 1))


def step(machine, runtime, project, dt_ms, events=None):
    """Advance the virtual clock by dt_ms, consuming timestamped events.

    events: [{'at_ms': int, 'event_id': str, 'phase': 'press'|'release'}],
    with at_ms relative to the runtime clock before this call.
    Returns a new runtime with the full reason history.
    """
    machine, _ = validate_machine(machine)
    dt_ms = v.integer(dt_ms, '推进毫秒', 0, 100000000)
    runtime = deepcopy(runtime)
    runtime['_schedules'] = _schedules(project, machine)
    states = {state['id']: state for state in machine['states']}
    if runtime.get('state_id') not in states:
        runtime.update(initial_runtime(machine))
        runtime['frame_id'] = runtime['_schedules'][runtime['state_id']][0]['frame_id']
    base = runtime['time_ms']
    if not runtime['history']:
        sequence = runtime['_schedules'][runtime['state_id']]
        runtime['frame_id'] = sequence[0]['frame_id'] if sequence else None
        _trace(runtime, 'state_enter', {'to_state_id': runtime['state_id'], 'frame_id': runtime['frame_id'],
                                        'reason': '试演开始，进入入口状态'})
    for event in _check_events(machine, events or [], dt_ms):
        at = base + event['at_ms']
        event = dict(event, at_ms=at)
        if not event['known']:
            _advance(machine, runtime, states, at)
            _trace(runtime, 'unknown_event', {'event_id': event['event_id'], 'reason': '未定义的输入被忽略'})
            continue
        _advance(machine, runtime, states, at)
        runtime['last_event'] = {'event_id': event['event_id'], 'phase': event['phase'], 'at_ms': at}
        if event['phase'] == PHASE_PRESS:
            _store_buffer(runtime, event['event_id'], PHASE_PRESS, at)
            if event['event_id'] not in runtime['held']:
                runtime['held'].append(event['event_id'])
            _trace(runtime, 'input_press', {'event_id': event['event_id'],
                                            'reason': f'记录输入并暂存（{event["event_id"]} 按下）'})
        else:
            _store_buffer(runtime, event['event_id'], PHASE_RELEASE, at)
            _pop_buffer(runtime, event['event_id'], PHASE_PRESS)
            if event['event_id'] in runtime['held']:
                runtime['held'].remove(event['event_id'])
            _trace(runtime, 'input_release', {'event_id': event['event_id'],
                                              'reason': f'记录松开并清除按下暂存（{event["event_id"]} 松开）'})
        _resolve(machine, runtime, states, at)
    _advance(machine, runtime, states, base + dt_ms)
    _resolve(machine, runtime, states, base + dt_ms, trace_blocked=False)
    runtime.pop('_schedules', None)
    return runtime


def _advance(machine, runtime, states, until):
    guard = 0
    while runtime['time_ms'] < until:
        if guard > 100000:
            raise v.Invalid('状态机推进超过安全步数，请检查回退环')
        guard += 1
        state = states[runtime['state_id']]
        sequence = runtime['_schedules'][state['id']]
        total = sum(item['duration'] for item in sequence)
        if total <= 0:
            if state['mode'] == 'oneshot':
                raise v.Invalid('一次性状态引用的片段没有曝光时长，无法回退')
            runtime['time_ms'] = until
            continue
        target = runtime['state_elapsed'] + (until - runtime['time_ms'])
        if state['mode'] == 'oneshot' and target >= total:
            _walk_exposures(runtime, sequence, total)
            _trace(runtime, 'oneshot_complete', {'frame_id': runtime['frame_id'],
                                                 'reason': f'一次性动作“{state["name"]}”曝光结束（{total}ms）'})
            fallback_id = state['fallback_state_id']
            _enter(machine, runtime, fallback_id, 'fallback',
                   f'一次性动作结束，按配置回退到“{states[fallback_id]["name"]}”')
            # Buffered inputs must be re-evaluated immediately at the real fallback time.
            _resolve(machine, runtime, states, runtime['time_ms'], trace_blocked=False)
            continue
        # Walk the repeating exposure sequence so every frame change, including
        # loop wraps, is traced at its real exposure time.
        remaining = until - runtime['time_ms']
        elapsed = runtime['state_elapsed']
        while remaining > 0:
            index = _frame_index(sequence, elapsed)
            boundary = sum(item['duration'] for item in sequence[:index + 1])
            cycle = elapsed // total * total
            distance = cycle + boundary - elapsed
            if distance <= 0:
                distance = sequence[0]['duration']
            delta = min(remaining, distance)
            runtime['time_ms'] += delta
            remaining -= delta
            elapsed += delta
            runtime['state_elapsed'] = elapsed
            if delta == distance:
                next_id = sequence[(index + 1) % len(sequence)]['frame_id']
                runtime['frame_id'] = next_id
                _trace(runtime, 'frame_change', {'frame_id': next_id,
                                                 'reason': f'按真实曝光时间切换到帧（状态内{elapsed}ms）'})
        frame_id = frame_at(sequence, runtime['state_elapsed'])
        if frame_id != runtime['frame_id']:
            runtime['frame_id'] = frame_id
            _trace(runtime, 'frame_change', {'frame_id': frame_id,
                                             'reason': f'按真实曝光时间切换到帧（状态内{runtime["state_elapsed"]}ms）'})


def _frame_index(sequence, elapsed):
    total = sum(item['duration'] for item in sequence)
    position = elapsed % total
    boundary = 0
    for index, item in enumerate(sequence):
        boundary += item['duration']
        if position < boundary:
            return index
    return len(sequence) - 1


def _walk_exposures(runtime, sequence, total):
    elapsed = runtime['state_elapsed']
    boundary = 0
    for item in sequence:
        boundary += item['duration']
        if boundary <= elapsed:
            continue
        delta = boundary - elapsed
        runtime['time_ms'] += delta
        elapsed = boundary
        runtime['state_elapsed'] = elapsed
        runtime['frame_id'] = item['frame_id']
        _trace(runtime, 'frame_change', {'frame_id': item['frame_id'],
                                         'reason': f'按真实曝光时间切换到帧（状态内{elapsed}ms）'})


def _resolve(machine, runtime, states, at, trace_blocked=True):
    # Buffers are owned by the state in which the input was buffered; only the
    # windows of transitions leaving that state can consume or expire them.
    for item in list(runtime['buffers']):
        windows = [transition['buffer_window'] for transition in machine['transitions']
                   if transition['from_state_id'] == item['state_id']
                   and transition['event_id'] == item['event_id'] and transition['phase'] == item['phase']]
        if not windows or at - item['at_ms'] > max(windows):
            window = max(windows, default=0)
            runtime['buffers'].remove(item)
            _trace(runtime, 'buffer_expired', {'event_id': item['event_id'], 'phase': item['phase'],
                                               'reason': f'暂存输入超过{window}ms窗口，已丢弃（{item["event_id"]}）'})
    state = states[runtime['state_id']]
    chosen, rejected = _choose(machine, state, runtime, at, trace_blocked)
    for item in rejected:
        _trace(runtime, 'transition_blocked', item)
    if chosen is None:
        return
    transition, reason = chosen
    target = states[transition['to_state_id']]
    _pop_buffer(runtime, transition['event_id'], transition['phase'])
    _trace(runtime, 'transition_selected', {'transition_id': transition['id'], 'event_id': transition['event_id'],
                                            'phase': transition['phase'], 'priority': transition['priority'],
                                            'reason': reason})
    _enter(machine, runtime, target['id'], 'state_enter',
           f'输入“{transition["event_id"]}”触发：{state["name"]} → {target["name"]}')


def simulate(machine, project, script):
    """Replay a recorded event script from time zero; fully deterministic."""
    machine, _ = validate_machine(machine)
    payload = script if isinstance(script, dict) else {'events': script}
    tail = v.integer(payload.get('tail_ms', 0), '试演尾部时长', 0, 100000000)
    runtime = initial_runtime(machine)
    runtime['_schedules'] = _schedules(project, machine)
    runtime['frame_id'] = runtime['_schedules'][runtime['state_id']][0]['frame_id']
    _trace(runtime, 'state_enter', {'to_state_id': runtime['state_id'], 'frame_id': runtime['frame_id'],
                                    'reason': '试演开始，进入入口状态'})
    states = {state['id']: state for state in machine['states']}
    events = _check_events(machine, payload.get('events', []), 100000000)
    horizon = max((event['at_ms'] for event in events), default=0) + tail
    for event in events:
        at = event['at_ms']
        _advance(machine, runtime, states, at)
        runtime['last_event'] = {'event_id': event['event_id'], 'phase': event['phase'], 'at_ms': at}
        if not event['known']:
            _trace(runtime, 'unknown_event', {'event_id': event['event_id'], 'reason': '未定义的输入被忽略'})
            continue
        if event['phase'] == PHASE_PRESS:
            _store_buffer(runtime, event['event_id'], PHASE_PRESS, at)
            if event['event_id'] not in runtime['held']:
                runtime['held'].append(event['event_id'])
            _trace(runtime, 'input_press', {'event_id': event['event_id'],
                                            'reason': f'记录输入并暂存（{event["event_id"]} 按下）'})
        else:
            _store_buffer(runtime, event['event_id'], PHASE_RELEASE, at)
            _pop_buffer(runtime, event['event_id'], PHASE_PRESS)
            if event['event_id'] in runtime['held']:
                runtime['held'].remove(event['event_id'])
            _trace(runtime, 'input_release', {'event_id': event['event_id'],
                                              'reason': f'记录松开并清除按下暂存（{event["event_id"]} 松开）'})
        _resolve(machine, runtime, states, at)
    _advance(machine, runtime, states, horizon)
    _resolve(machine, runtime, states, horizon, trace_blocked=False)
    runtime.pop('_schedules', None)
    return runtime


def save(store, key, revision, body, identifier=None):
    with store.edit(key, revision, '保存动作状态机') as project:
        collection = project.setdefault('state_machines', [])
        checked, clip_ids = validate_machine(body)
        known = {clip['id'] for clip in project['clips']}
        if not clip_ids <= known:
            raise v.Invalid('状态引用了不存在的动作片段，请先保存片段')
        if identifier:
            machine = next((item for item in collection if item['id'] == identifier), None)
            if machine is None:
                raise v.Invalid('动作状态机不存在')
            checked['id'] = machine['id']
            collection[collection.index(machine)] = checked
        else:
            if len(collection) >= MAX_MACHINES:
                raise v.Invalid(f'每个项目最多{MAX_MACHINES}个动作状态机')
            checked['id'] = new_id()
            collection.append(checked)
        result = deepcopy(checked)
    return store.get(key), result


def delete(store, key, revision, identifier):
    with store.edit(key, revision, '删除动作状态机') as project:
        collection = project.setdefault('state_machines', [])
        machine = next((item for item in collection if item['id'] == identifier), None)
        if machine is None:
            raise v.Invalid('动作状态机不存在')
        collection.remove(machine)
    return store.get(key)


def used_clips(project):
    return {state['clip_id'] for machine in project.get('state_machines', []) for state in machine['states']}


def example_machine(project):
    """Build a starter idle/walk/attack machine from clips whose names hint at them."""
    def find(*keywords):
        for clip in project['clips']:
            name = clip['name'].casefold()
            if all(word in name for word in keywords):
                return clip
        return None
    idle = find('待机') or find('idle') or (project['clips'][0] if project['clips'] else None)
    walk = find('行走') or find('walk') or find('走')
    attack = find('攻击') or find('attack') or find('攻')
    if not idle or not walk or not attack:
        raise v.Invalid('示例需要待机、行走、攻击三个片段，请先在时间轴保存对应片段')
    idle_id, walk_id, attack_id = new_id(), new_id(), new_id()
    t1, t2, t3, t4, t5 = (new_id() for _ in range(5))
    attack_total = sum(find_frame(project, frame_id)['duration'] for frame_id in attack['frame_ids'])
    return {
        'name': '待机/行走/攻击试演',
        'events': [
            {'id': 'move', 'name': '移动', 'key': 'Shift'},
            {'id': 'attack', 'name': '攻击', 'key': 'Space'},
        ],
        'states': [
            {'id': idle_id, 'name': '待机', 'clip_id': idle['id'], 'mode': 'loop', 'uninterruptible': 0},
            {'id': walk_id, 'name': '行走', 'clip_id': walk['id'], 'mode': 'loop', 'uninterruptible': 0},
            {'id': attack_id, 'name': '攻击', 'clip_id': attack['id'], 'mode': 'oneshot',
             'uninterruptible': max(1, attack_total // 3), 'fallback_state_id': idle_id},
        ],
        'transitions': [
            {'id': t1, 'from_state_id': idle_id, 'to_state_id': walk_id, 'event_id': 'move',
             'phase': 'press', 'priority': 10, 'buffer_window': 200},
            {'id': t2, 'from_state_id': walk_id, 'to_state_id': idle_id, 'event_id': 'move',
             'phase': 'release', 'priority': 10, 'buffer_window': 0},
            {'id': t3, 'from_state_id': idle_id, 'to_state_id': attack_id, 'event_id': 'attack',
             'phase': 'press', 'priority': 20, 'buffer_window': 300},
            {'id': t4, 'from_state_id': walk_id, 'to_state_id': attack_id, 'event_id': 'attack',
             'phase': 'press', 'priority': 20, 'buffer_window': 300},
            {'id': t5, 'from_state_id': attack_id, 'to_state_id': attack_id, 'event_id': 'attack',
             'phase': 'press', 'priority': 20, 'buffer_window': 120},
        ],
        'initial_state_id': idle_id,
    }
