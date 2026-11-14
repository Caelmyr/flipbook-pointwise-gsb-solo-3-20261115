/* 动作状态机工作区：本地实时试演引擎（规则与后台 flipbook/statemachine.py 一致），
   配置始终保存到后台；录制脚本可交后台确定性复算，核对两端结果。 */
(function () {
  'use strict';
  const PRESS = 'press', RELEASE = 'release';
  const MAX_HISTORY = 200;
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = text; if (cls) e.className = cls; return e; };
  const newId = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join(''));
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

  /* ---------------- 曝光计划：复用与后台一致的片段方向规则 ---------------- */
  function clipSequence(project, clipId) {
    const clip = project.clips.find(c => c.id === clipId);
    if (!clip) throw new Error('状态引用的片段不存在：' + clipId);
    let items = clip.exposures
      ? clip.exposures.map(x => ({ frame_id: x.frame_id, duration: Number(x.duration) }))
      : clip.frame_ids.map(id => ({ frame_id: id, duration: Number(project.frames.find(f => f.id === id).duration) }));
    if (clip.direction === 'reverse') items = items.slice().reverse();
    else if (clip.direction === 'pingpong') items = items.concat(items.slice(-2, 0, -1).map(x => ({ ...x })));
    return items;
  }
  function frameAt(sequence, elapsed) {
    const total = sequence.reduce((a, x) => a + x.duration, 0);
    let pos = elapsed % total;
    for (const item of sequence) { if (pos < item.duration) return item.frame_id; pos -= item.duration; }
    return sequence[sequence.length - 1].frame_id;
  }

  /* ---------------- 本地实时引擎（虚拟毫秒时钟） ---------------- */
  function initialRuntime(machine) {
    return { time_ms: 0, state_id: machine.initial_state_id, state_elapsed: 0, frame_id: null, buffers: [], held: [], last_event: null, history: [] };
  }
  function trace(rt, code, detail) {
    const entry = { time_ms: rt.time_ms, state_id: rt.state_id, state_elapsed: rt.state_elapsed, code, ...detail };
    rt.history.push(entry);
    if (rt.history.length > MAX_HISTORY) rt.history.splice(0, rt.history.length - MAX_HISTORY);
  }
  const bkey = (eventId, phase) => eventId + '|' + phase;
  function storeBuffer(rt, eventId, phase, at) {
    const key = bkey(eventId, phase);
    rt.buffers = rt.buffers.filter(b => b.key !== key);
    rt.buffers.push({ key, event_id: eventId, phase, at_ms: at, state_id: rt.state_id });
  }
  function popBuffer(rt, eventId, phase) {
    rt.buffers = rt.buffers.filter(b => !(b.event_id === eventId && (phase === undefined || b.phase === phase)));
  }
  function stateMap(machine) { return Object.fromEntries(machine.states.map(s => [s.id, s])); }
  function enter(machine, rt, schedules, stateId, code, reason) {
    rt.state_id = stateId; rt.state_elapsed = 0;
    const seq = schedules[stateId];
    rt.frame_id = seq.length ? seq[0].frame_id : null;
    trace(rt, code, { to_state_id: stateId, frame_id: rt.frame_id, reason });
  }
  function eligible(machine, state, tr, rt, at) {
    if (tr.from_state_id !== state.id) return { ok: false, reason: '来源状态不匹配' };
    const buffered = rt.buffers.find(b => b.key === bkey(tr.event_id, tr.phase));
    if (!buffered) return { ok: false, reason: '没有暂存输入：' + tr.event_id };
    const age = at - buffered.at_ms;
    if (age > tr.buffer_window) return { ok: false, reason: `暂存输入已过期（${age}ms > ${tr.buffer_window}ms）` };
    if (rt.state_elapsed < state.uninterruptible) return { ok: false, reason: `不可打断区间内（${rt.state_elapsed}ms < ${state.uninterruptible}ms）` };
    return { ok: true, reason: `命中切换，输入暂存${age}ms，优先级${tr.priority}` };
  }
  function choose(machine, state, rt, at, traceBlocked = true) {
    const candidates = [], rejected = [];
    machine.transitions.forEach((tr, index) => {
      const result = eligible(machine, state, tr, rt, at);
      if (result.ok) candidates.push({ index, tr, reason: result.reason });
      else if (traceBlocked && tr.from_state_id === state.id) rejected.push({ transition_id: tr.id, event_id: tr.event_id, phase: tr.phase, reason: result.reason });
    });
    if (!candidates.length) return { winner: null, rejected };
    candidates.sort((a, b) => (b.tr.priority - a.tr.priority) || (a.index - b.index));
    const winner = candidates[0];
    candidates.slice(1).forEach(c => rejected.push({ transition_id: c.tr.id, event_id: c.tr.event_id, phase: c.tr.phase, reason: '同刻多条件命中，优先级较低或排序靠后' }));
    return { winner, rejected };
  }
  function frameIndex(sequence, elapsed) {
    const total = sequence.reduce((a, x) => a + x.duration, 0);
    const position = elapsed % total;
    let boundary = 0;
    for (let i = 0; i < sequence.length; i++) {
      boundary += sequence[i].duration;
      if (position < boundary) return i;
    }
    return sequence.length - 1;
  }
  function walkExposures(rt, sequence) {
    const total = sequence.reduce((a, x) => a + x.duration, 0);
    let elapsed = rt.state_elapsed, boundary = 0;
    for (const item of sequence) {
      boundary += item.duration;
      if (boundary <= elapsed) continue;
      const delta = boundary - elapsed;
      rt.time_ms += delta;
      elapsed = boundary;
      rt.state_elapsed = elapsed;
      rt.frame_id = item.frame_id;
      trace(rt, 'frame_change', { frame_id: item.frame_id, reason: `按真实曝光时间切换到帧（状态内${elapsed}ms）` });
    }
    return total;
  }
  function advance(machine, rt, states, schedules, until) {
    let guard = 0;
    while (rt.time_ms < until) {
      if (++guard > 100000) throw new Error('状态机推进超过安全步数，请检查回退环');
      const state = states[rt.state_id];
      const sequence = schedules[state.id];
      const total = sequence.reduce((a, x) => a + x.duration, 0);
      if (total <= 0) {
        if (state.mode === 'oneshot') throw new Error('一次性状态引用的片段没有曝光时长，无法回退');
        rt.time_ms = until;
        continue;
      }
      const target = rt.state_elapsed + (until - rt.time_ms);
      if (state.mode === 'oneshot' && target >= total) {
        walkExposures(rt, sequence);
        trace(rt, 'oneshot_complete', { frame_id: rt.frame_id, reason: `一次性动作“${state.name}”曝光结束（${total}ms）` });
        const fallback = state.fallback_state_id;
        enter(machine, rt, schedules, fallback, 'fallback', `一次性动作结束，按配置回退到“${states[fallback].name}”`);
        resolve(machine, rt, states, schedules, rt.time_ms, false);
        continue;
      }
      let remaining = until - rt.time_ms;
      let elapsed = rt.state_elapsed;
      while (remaining > 0) {
        const index = frameIndex(sequence, elapsed);
        const boundary = sequence.slice(0, index + 1).reduce((a, x) => a + x.duration, 0);
        const cycle = Math.floor(elapsed / total) * total;
        let distance = cycle + boundary - elapsed;
        if (distance <= 0) distance = sequence[0].duration;
        const delta = Math.min(remaining, distance);
        rt.time_ms += delta;
        remaining -= delta;
        elapsed += delta;
        rt.state_elapsed = elapsed;
        if (delta === distance) {
          const nextId = sequence[(index + 1) % sequence.length].frame_id;
          rt.frame_id = nextId;
          trace(rt, 'frame_change', { frame_id: nextId, reason: `按真实曝光时间切换到帧（状态内${elapsed}ms）` });
        }
      }
      const frameId = frameAt(sequence, rt.state_elapsed);
      if (frameId !== rt.frame_id) {
        rt.frame_id = frameId;
        trace(rt, 'frame_change', { frame_id: frameId, reason: `按真实曝光时间切换到帧（状态内${rt.state_elapsed}ms）` });
      }
    }
  }
  function resolve(machine, rt, states, schedules, at, traceBlocked = true) {
    for (const item of rt.buffers.slice()) {
      const windows = machine.transitions
        .filter(t => t.from_state_id === item.state_id && t.event_id === item.event_id && t.phase === item.phase)
        .map(t => t.buffer_window);
      const window = Math.max(0, ...windows);
      if (!windows.length || at - item.at_ms > window) {
        rt.buffers.splice(rt.buffers.indexOf(item), 1);
        trace(rt, 'buffer_expired', { event_id: item.event_id, phase: item.phase, reason: `暂存输入超过${window}ms窗口，已丢弃（${item.event_id}）` });
      }
    }
    const state = states[rt.state_id];
    const { winner, rejected } = choose(machine, state, rt, at, traceBlocked);
    rejected.forEach(r => trace(rt, 'transition_blocked', r));
    if (!winner) return;
    const { tr, reason } = winner;
    popBuffer(rt, tr.event_id, tr.phase);
    trace(rt, 'transition_selected', { transition_id: tr.id, event_id: tr.event_id, phase: tr.phase, priority: tr.priority, reason });
    enter(machine, rt, schedules, tr.to_state_id, 'state_enter', `输入“${tr.event_id}”触发：${state.name} → ${states[tr.to_state_id].name}`);
  }
  /* 本地引擎入口：dt 为本次推进毫秒；events 的 at_ms 相对本次调用前的时钟 */
  function step(machine, rtIn, project, dt, events = []) {
    const rt = structuredClone(rtIn);
    const states = stateMap(machine);
    if (!states[rt.state_id]) Object.assign(rt, initialRuntime(machine));
    const schedules = Object.fromEntries(machine.states.map(s => [s.id, clipSequence(project, s.clip_id)]));
    if (rt.frame_id === null) rt.frame_id = schedules[rt.state_id][0].frame_id;
    const base = rt.time_ms;
    if (!rt.history.length) {
      rt.frame_id = schedules[rt.state_id][0].frame_id;
      trace(rt, 'state_enter', { to_state_id: rt.state_id, frame_id: rt.frame_id, reason: '试演开始，进入入口状态' });
    }
    const checked = events.map(e => ({ ...e, at_ms: base + Number(e.at_ms) }))
      .sort((a, b) => (a.at_ms - b.at_ms) || (a.phase === PRESS ? -1 : 1));
    const known = new Set(machine.events.map(e => e.id));
    for (const ev of checked) {
      advance(machine, rt, states, schedules, ev.at_ms);
      rt.last_event = { event_id: ev.event_id, phase: ev.phase, at_ms: ev.at_ms };
      if (!known.has(ev.event_id)) { trace(rt, 'unknown_event', { event_id: ev.event_id, reason: '未定义的输入被忽略' }); continue; }
      if (ev.phase === PRESS) {
        storeBuffer(rt, ev.event_id, PRESS, ev.at_ms);
        if (!rt.held.includes(ev.event_id)) rt.held.push(ev.event_id);
        trace(rt, 'input_press', { event_id: ev.event_id, reason: `记录输入并暂存（${ev.event_id} 按下）` });
      } else {
        storeBuffer(rt, ev.event_id, RELEASE, ev.at_ms);
        popBuffer(rt, ev.event_id, PRESS);
        rt.held = rt.held.filter(id => id !== ev.event_id);
        trace(rt, 'input_release', { event_id: ev.event_id, reason: `记录松开并清除按下暂存（${ev.event_id} 松开）` });
      }
      resolve(machine, rt, states, schedules, ev.at_ms);
    }
    advance(machine, rt, states, schedules, base + Number(dt));
    resolve(machine, rt, states, schedules, base + Number(dt), false);
    return rt;
  }

  /* ---------------- 工作区 ---------------- */
  const UI = {
    machines: [], machine: null, machineId: null, project: null,
    runtime: null, playing: false, raf: null, lastTick: 0,
    script: [], keyDown: new Set(), auditionToken: 0,
  };
  function apiCall(path, options = {}) { return api(path, options); }
  function postCall(path, data = {}, method = 'POST') { return post(path, data, method); }
  function guardCall(fn) { return guard(fn); }
  function statusCall(msg, error) { status(msg, Boolean(error)); }

  if (typeof document !== 'undefined') $('stateMachine').onclick = guardCall(openWorkspace);

  async function openWorkspace() {
    if (!S.project) throw new Error('请先选择动画项目');
    const body = dialog('动作状态机工作区 · 打断/暂存/回退试演');
    body.classList.add('sm-body');
    $('dialog').addEventListener('close', stopAudition, { once: true });
    UI.project = S.project;
    UI.machines = (await apiCall(endpoint('state-machines'))).machines;
    UI.machine = UI.machines[0] || null;
    UI.machineId = UI.machine?.id || null;
    render(body);
  }
  const endpoint = (path = '') => `/api/projects/${UI.project.id}` + (path ? '/' + path : '');

  function render(body) {
    body.replaceChildren();
    const top = el('div', undefined, 'sm-topbar');
    const chooser = el('select');
    chooser.append(new Option('＋ 新建动作状态机', ''));
    UI.machines.forEach(m => chooser.append(new Option(m.name, m.id)));
    chooser.value = UI.machineId || '';
    chooser.onchange = () => { stopAudition(); UI.machineId = chooser.value; UI.machine = UI.machines.find(m => m.id === UI.machineId) || null; render(body); };
    top.append(chooser);
    const example = el('button', '按片段名生成待机/行走/攻击示例');
    example.onclick = guardCall(async () => {
      const sample = await postCall(endpoint('state-machines/example'), {});
      UI.machine = normalize(sample); UI.machineId = null; render(body);
      statusCall('示例已填入编辑器，保存后即可试演');
    });
    top.append(example);
    body.append(top);
    if (!UI.machine) {
      body.append(el('p', '先在时间轴用“保存所选片段”建立待机、行走、攻击等片段，然后新建状态机把片段接成状态；也可以一键生成示例。', 'hint'));
      UI.machine = blankMachine();
      renderEditor(body);
      return;
    }
    const layout = el('div', undefined, 'sm-layout');
    layout.append(renderEditor(body), renderAudition());
    body.append(layout);
  }

  function blankMachine() {
    const initial = newId();
    const clip = UI.project.clips[0];
    return {
      name: '新动作状态机',
      events: [{ id: 'action', name: '动作输入', key: 'Space' }],
      states: clip ? [{ id: initial, name: '状态1', clip_id: clip.id, mode: 'loop', uninterruptible: 0 }] : [],
      transitions: [], initial_state_id: initial,
    };
  }
  function normalize(machine) {
    return {
      name: machine.name || '新动作状态机',
      events: machine.events || [], states: machine.states || [],
      transitions: machine.transitions || [], initial_state_id: machine.initial_state_id,
    };
  }

  /* ---------------- 编辑器 ---------------- */
  function renderEditor(body) {
    const panel = el('div', undefined, 'sm-editor');
    panel.append(el('h3', '① 配置状态与切换（保存到后台，重开页面仍可继续试演）'));
    const nameRow = el('label', undefined, 'sm-row');
    nameRow.append(el('span', '状态机名称'), textInput(UI.machine.name, v => { UI.machine.name = v; }));
    panel.append(nameRow);

    panel.append(el('h4', '输入定义（试演按键 → 输入标识）'));
    const eventRows = el('div', undefined, 'sm-table');
    UI.machine.events.forEach((event, i) => {
      const row = el('div', undefined, 'sm-tr');
      row.append(
        boundInput(event, 'name', '名称', i, eventRows),
        boundInput(event, 'id', '标识', i, eventRows),
        boundInput(event, 'key', '按键', i, eventRows),
        removeButton(() => { UI.machine.events.splice(i, 1); render(body); }));
      eventRows.append(row);
    });
    panel.append(eventRows, addButton('＋ 添加输入', () => { UI.machine.events.push({ id: 'input' + (UI.machine.events.length + 1), name: '新输入', key: 'Key' + (UI.machine.events.length + 1) }); render(body); }));

    panel.append(el('h4', '状态（片段 + 播放方式 + 不可打断区间 + 回退）'));
    const stateRows = el('div', undefined, 'sm-table');
    UI.machine.states.forEach((state, i) => {
      const row = el('div', undefined, 'sm-tr');
      row.append(boundInput(state, 'name', '状态名', i, stateRows));
      const clipSelect = el('select');
      UI.project.clips.forEach(c => clipSelect.append(new Option(c.name, c.id)));
      clipSelect.value = state.clip_id;
      clipSelect.onchange = () => { state.clip_id = clipSelect.value; };
      const modeSelect = el('select');
      [['loop', '循环（待机/行走）'], ['oneshot', '一次性（攻击）']].forEach(([value, label]) => modeSelect.append(new Option(label, value)));
      modeSelect.value = state.mode;
      modeSelect.onchange = () => { state.mode = modeSelect.value; if (state.mode === 'oneshot' && !state.fallback_state_id) state.fallback_state_id = UI.machine.states[0]?.id; render(body); };
      const num = numberInput(state.uninterruptible, 0, 1000000, v => { state.uninterruptible = v; });
      num.title = '进入状态后多少毫秒内拒绝一切切换';
      const fallback = el('select');
      fallback.append(new Option('（不回退）', ''));
      UI.machine.states.forEach(s => fallback.append(new Option(s.name, s.id)));
      fallback.value = state.fallback_state_id || '';
      fallback.onchange = () => { state.fallback_state_id = fallback.value || undefined; };
      row.append(clipSelect, modeSelect, num, fallback, removeButton(() => { removeState(i, body); }));
      stateRows.append(row);
    });
    panel.append(stateRows, addButton('＋ 添加状态', () => {
      const clip = UI.project.clips[0];
      if (!clip) throw new Error('请先在时间轴保存动作片段');
      const id = newId();
      UI.machine.states.push({ id, name: '状态' + (UI.machine.states.length + 1), clip_id: clip.id, mode: 'loop', uninterruptible: 0 });
      render(body);
    }));

    panel.append(el('h4', '切换条件（同刻多条件命中时，取优先级最大；相同优先级取排序靠前者）'));
    const trRows = el('div', undefined, 'sm-table');
    UI.machine.transitions.forEach((tr, i) => {
      const row = el('div', undefined, 'sm-tr');
      const from = stateSelect(tr.from_state_id, v => { tr.from_state_id = v; });
      const to = stateSelect(tr.to_state_id, v => { tr.to_state_id = v; });
      const eventSel = el('select');
      UI.machine.events.forEach(e => eventSel.append(new Option(`${e.name}（${e.id}）`, e.id)));
      eventSel.value = tr.event_id;
      eventSel.onchange = () => { tr.event_id = eventSel.value; };
      const phase = el('select');
      phase.append(new Option('按下 press', PRESS), new Option('松开 release', RELEASE));
      phase.value = tr.phase;
      phase.onchange = () => { tr.phase = phase.value; };
      const prio = numberInput(tr.priority, 0, 100000, v => { tr.priority = v; });
      const buffer = numberInput(tr.buffer_window, 0, 10000, v => { tr.buffer_window = v; });
      row.append(from, eventSel, phase, to, prio, buffer, removeButton(() => { UI.machine.transitions.splice(i, 1); render(body); }));
      trRows.append(row);
    });
    panel.append(trRows, addButton('＋ 添加切换条件', () => {
      const first = UI.machine.states[0];
      if (!first) throw new Error('请先添加状态');
      UI.machine.transitions.push({ id: newId(), from_state_id: first.id, to_state_id: first.id, event_id: UI.machine.events[0]?.id || 'action', phase: PRESS, priority: 10, buffer_window: 300 });
      render(body);
    }));
    panel.append(el('p', '说明：一次性状态必须选择回退状态，曝光播完自动回退；不可打断区间按片段真实帧曝光毫秒计算；输入在暂存窗口内保留，窗口外确定性丢弃。', 'hint'));

    const initialRow = el('div', undefined, 'sm-row');
    initialRow.append(el('span', '入口状态'), stateSelect(UI.machine.initial_state_id, v => { UI.machine.initial_state_id = v; }));
    const save = el('button', '保存状态机', 'primary');
    save.onclick = guardCall(async () => {
      validateLocal(UI.machine);
      stopAudition();
      const payload = { revision: S.project.revision, machine: UI.machine };
      if (UI.machineId) {
        const result = await postCall(endpoint('state-machines/' + UI.machineId), payload, 'PUT');
        UI.machine = normalize(result.machine);
        await acceptProject(result.project);
      } else {
        const result = await postCall(endpoint('state-machines'), payload, 'PUT');
        UI.machine = normalize(result.machine); UI.machineId = result.machine.id;
        await acceptProject(result.project);
      }
      UI.project = S.project;
      UI.machines = (await apiCall(endpoint('state-machines'))).machines;
      statusCall('动作状态机已保存，可开始试演');
      render(body);
    });
    const del = el('button', '删除此状态机', 'primary');
    del.onclick = guardCall(async () => {
      if (!UI.machineId) { UI.machine = blankMachine(); render(body); return; }
      const result = await postCall(endpoint('state-machines/' + UI.machineId),
        { revision: S.project.revision }, 'DELETE');
      stopAudition();
      await acceptProject(result);
      UI.project = S.project;
      UI.machines = (await apiCall(endpoint('state-machines'))).machines;
      UI.machine = UI.machines[0] || null; UI.machineId = UI.machine?.id || null;
      render(body);
    });
    panel.append(initialRow);
    const actions = el('div', undefined, 'sm-actions');
    actions.append(save, del);
    panel.append(actions);
    return panel;
  }

  function validateLocal(machine) {
    if (!machine.states.length) throw new Error('至少需要一个状态');
    if (!machine.states.some(s => s.id === machine.initial_state_id)) throw new Error('入口状态不存在');
    const stateIds = new Set(machine.states.map(s => s.id));
    const eventIds = new Set(machine.events.map(e => e.id));
    for (const s of machine.states) {
      if (!UI.project.clips.some(c => c.id === s.clip_id)) throw new Error(`状态“${s.name}”引用的片段不存在`);
      if (s.mode === 'oneshot' && !s.fallback_state_id) throw new Error(`一次性状态“${s.name}”必须指定回退状态`);
      if (s.fallback_state_id && !stateIds.has(s.fallback_state_id)) throw new Error(`状态“${s.name}”的回退状态不存在`);
    }
    for (const t of machine.transitions) {
      if (!stateIds.has(t.from_state_id) || !stateIds.has(t.to_state_id)) throw new Error('切换条件引用了不存在的状态');
      if (!eventIds.has(t.event_id)) throw new Error('切换条件引用了未定义的输入：' + t.event_id);
    }
  }
  function removeState(index, body) {
    const removed = UI.machine.states[index];
    if (UI.machine.states.length === 1) throw new Error('至少保留一个状态');
    UI.machine.states.splice(index, 1);
    UI.machine.transitions = UI.machine.transitions.filter(t => t.from_state_id !== removed.id && t.to_state_id !== removed.id);
    UI.machine.states.forEach(s => { if (s.fallback_state_id === removed.id) s.fallback_state_id = undefined; });
    if (UI.machine.initial_state_id === removed.id) UI.machine.initial_state_id = UI.machine.states[0].id;
    render(body);
  }

  /* ---------------- 试演 ---------------- */
  function renderAudition() {
    const panel = el('div', undefined, 'sm-audition');
    panel.append(el('h3', '② 按键试演（真实曝光时间 · 切换理由实时记录）'));
    const preview = el('div', undefined, 'sm-preview');
    const img = el('img');
    img.alt = '动作状态机试演画面';
    img.style.imageRendering = 'pixelated';
    const stateBadge = el('div', undefined, 'sm-badge');
    preview.append(img, stateBadge);
    panel.append(preview);

    const meters = el('div', undefined, 'sm-meters');
    const timeMeter = el('div');
    const stateMeter = el('div');
    const frameMeter = el('div');
    meters.append(timeMeter, stateMeter, frameMeter);
    const heldBox = el('div', undefined, 'sm-held');
    panel.append(meters, heldBox);

    const controls = el('div', undefined, 'sm-row');
    const playBtn = el('button', '▶ 开始试演', 'primary');
    const resetBtn = el('button', '⟲ 复位时钟');
    const stepBtn = el('button', '单步 100ms');
    const keyHint = el('span', '焦点在本面板时按定义的按键；Shift/Ctrl 等修饰键也可识别', 'muted');
    controls.append(playBtn, resetBtn, stepBtn, keyHint);
    panel.append(controls);

    const keyButtons = el('div', undefined, 'sm-keys');
    UI.machine.events.forEach(event => {
      const b = el('button', `${event.name} [${event.key}]`);
      b.onmousedown = e => { e.preventDefault(); feed(event.id, PRESS); };
      b.onmouseup = e => { e.preventDefault(); feed(event.id, RELEASE); };
      b.onmouseleave = () => { if (UI.keyDown.has(event.id)) feed(event.id, RELEASE); };
      keyButtons.append(b);
    });
    panel.append(keyButtons);

    const log = el('div', undefined, 'sm-log');
    panel.append(el('h4', '切换理由（最近 200 条，含被阻止/暂存过期/回退原因）'), log);

    const replay = el('div', undefined, 'sm-row');
    const replayBtn = el('button', '把录制脚本交后台确定性复算');
    const downloadBtn = el('button', '下载试演脚本 JSON');
    replay.append(replayBtn, downloadBtn);
    panel.append(replay);

    function ensureRuntime() {
      if (!UI.runtime) {
        UI.runtime = initialRuntime(UI.machine);
        const schedules = Object.fromEntries(UI.machine.states.map(s => [s.id, clipSequence(UI.project, s.clip_id)]));
        UI.runtime.frame_id = schedules[UI.machine.initial_state_id][0].frame_id;
        trace(UI.runtime, 'state_enter', { to_state_id: UI.machine.initial_state_id, frame_id: UI.runtime.frame_id, reason: '试演开始，进入入口状态' });
      }
    }
    function refresh(forceFrame) {
      ensureRuntime();
      const state = UI.machine.states.find(s => s.id === UI.runtime.state_id);
      timeMeter.textContent = `虚拟时钟 ${UI.runtime.time_ms} ms`;
      stateMeter.textContent = `当前状态：${state?.name || '—'}（状态内 ${UI.runtime.state_elapsed} ms${state && state.uninterruptible ? ` / 不可打断 ${state.uninterruptible} ms` : ''}）`;
      const frame = UI.project.frames.find(f => f.id === UI.runtime.frame_id);
      frameMeter.textContent = `当前帧：${frame ? frame.name + ' · ' + frame.duration + 'ms' : '—'}`;
      stateBadge.textContent = state ? state.name : '';
      stateBadge.classList.toggle('sm-uninterruptible', Boolean(state && UI.runtime.state_elapsed < state.uninterruptible));
      heldBox.textContent = '按住中的输入：' + (UI.runtime.held.length ? UI.runtime.held.join('、') : '无') +
        ' ｜ 暂存队列：' + (UI.runtime.buffers.length ? UI.runtime.buffers.map(b => `${b.event_id}:${b.phase}(${UI.runtime.time_ms - b.at_ms}ms)`).join('，') : '空');
      if (forceFrame || img.dataset.frame !== UI.runtime.frame_id) {
        img.dataset.frame = UI.runtime.frame_id;
        img.src = `/api/projects/${UI.project.id}/frames/${UI.runtime.frame_id}/image?rev=${UI.project.revision}&t=${UI.runtime.time_ms}`;
      }
      renderLog(log);
    }
    function renderLog(target) {
      target.replaceChildren();
      UI.runtime.history.slice(-60).forEach(h => {
        const row = el('div', undefined, 'sm-logline sm-log-' + h.code);
        row.append(el('b', String(h.time_ms) + 'ms'), el('span', CODE_LABEL[h.code] || h.code), el('p', h.reason || ''));
        target.append(row);
      });
      target.scrollTop = target.scrollHeight;
    }
    function feed(eventId, phase) {
      if (phase === PRESS) {
        if (!UI.playing) start();
        if (UI.keyDown.has(eventId)) return;
        UI.keyDown.add(eventId);
      } else {
        if (!UI.keyDown.has(eventId)) return;
        UI.keyDown.delete(eventId);
      }
      ensureRuntime();
      UI.script.push({ at_ms: UI.runtime.time_ms, event_id: eventId, phase });
      UI.runtime = step(UI.machine, UI.runtime, UI.project, 0, [{ at_ms: 0, event_id: eventId, phase }]);
      refresh(true);
    }
    function tick(now) {
      if (!UI.playing) return;
      const dt = Math.min(100, now - UI.lastTick);
      UI.lastTick = now;
      UI.runtime = step(UI.machine, UI.runtime, UI.project, Math.round(dt), []);
      refresh(false);
      UI.raf = requestAnimationFrame(tick);
    }
    function start() {
      ensureRuntime();
      UI.playing = true; UI.lastTick = performance.now();
      playBtn.textContent = 'Ⅱ 暂停';
      UI.raf = requestAnimationFrame(tick);
    }
    function stopAuditionLocal() {
      UI.playing = false; if (UI.raf) cancelAnimationFrame(UI.raf); UI.raf = null;
      playBtn.textContent = '▶ 开始试演'; UI.keyDown.clear();
    }
    playBtn.onclick = () => { UI.playing ? stopAuditionLocal() : start(); };
    resetBtn.onclick = () => {
      stopAuditionLocal(); UI.runtime = null; UI.script = []; UI.keyDown.clear(); refresh(true);
    };
    stepBtn.onclick = () => { ensureRuntime(); UI.runtime = step(UI.machine, UI.runtime, UI.project, 100, []); refresh(false); };
    panel.tabIndex = 0;
    panel.onkeydown = e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
      const event = matchKey(e);
      if (!event) return;
      e.preventDefault();
      feed(event.id, PRESS);
    };
    panel.onkeyup = e => {
      const event = matchKey(e);
      if (!event) return;
      e.preventDefault();
      feed(event.id, RELEASE);
    };
    function matchKey(e) {
      const candidates = [e.code, e.key, e.key.length === 1 ? e.key.toLowerCase() : null].filter(Boolean);
      return UI.machine.events.find(event => candidates.some(c => c.toLowerCase() === String(event.key).toLowerCase()));
    }
    replayBtn.onclick = guardCall(async () => {
      if (!UI.machineId) throw new Error('请先保存状态机再复算');
      if (!UI.runtime) throw new Error('先开始试演并产生输入，再复算');
      const result = await postCall(endpoint(`state-machines/${UI.machineId}/simulate`), { events: UI.script, tail_ms: 500 });
      const sameState = UI.runtime.state_id === result.runtime.state_id
        && UI.runtime.frame_id === result.runtime.frame_id
        && UI.runtime.time_ms === result.runtime.time_ms
        && JSON.stringify(UI.runtime.history) === JSON.stringify(result.runtime.history);
      showReplay(result.runtime, sameState);
      statusCall(sameState ? '后台复算与本地试演结果一致（含全部切换理由）' : '后台复算与本地结果不一致，请查看复算日志', !sameState);
    });
    downloadBtn.onclick = () => jsonDownload({ machine_id: UI.machineId, events: UI.script }, 'state-machine-audition.json');
    refresh(true);
    return panel;
  }
  const CODE_LABEL = {
    state_enter: '进入状态', frame_change: '帧切换', input_press: '按下', input_release: '松开',
    transition_selected: '采用切换', transition_blocked: '切换被阻止', buffer_expired: '暂存过期',
    oneshot_complete: '一次性结束', fallback: '自动回退', unknown_event: '未知输入',
  };
  function showReplay(runtime, sameState) {
    const box = el('div', undefined, 'sm-replay');
    box.append(el('h4', sameState ? '后台复算结果：与本地一致 ✔' : '后台复算结果：与本地不一致 ✘'));
    const summary = el('p', `终态时间 ${runtime.time_ms}ms · 状态 ${runtime.state_id.slice(0, 8)} · 帧 ${runtime.frame_id.slice(0, 8)}`, 'muted');
    box.append(summary);
    const log = el('div', undefined, 'sm-log');
    runtime.history.forEach(h => {
      const row = el('div', undefined, 'sm-logline sm-log-' + h.code);
      row.append(el('b', String(h.time_ms) + 'ms'), el('span', CODE_LABEL[h.code] || h.code), el('p', h.reason || ''));
      log.append(row);
    });
    box.append(log);
    const d = el('dialog');
    d.className = 'sm-replay-dialog';
    d.append(box);
    d.addEventListener('click', e => { if (e.target === d) d.close(); });
    document.body.append(d); d.showModal();
    d.addEventListener('close', () => d.remove());
  }
  function stopAudition() { UI.playing = false; if (UI.raf) cancelAnimationFrame(UI.raf); UI.raf = null; UI.runtime = null; UI.script = []; UI.keyDown.clear(); }

  /* ---------------- 小控件 ---------------- */
  function textInput(value, oninput) {
    const i = el('input'); i.value = value ?? ''; i.oninput = () => oninput(i.value); return i;
  }
  function numberInput(value, min, max, oninput) {
    const i = el('input'); i.type = 'number'; i.min = min; i.max = max; i.value = value ?? 0;
    i.oninput = () => { const n = Number(i.value); if (Number.isFinite(n)) oninput(n); };
    return i;
  }
  function boundInput(obj, key, placeholder, index, container) {
    const i = textInput(obj[key], v => { obj[key] = v; });
    i.placeholder = placeholder;
    return i;
  }
  function stateSelect(value, onchange) {
    const s = el('select');
    UI.machine.states.forEach(st => s.append(new Option(st.name, st.id)));
    s.value = value || '';
    s.onchange = () => onchange(s.value);
    return s;
  }
  function addButton(label, onclick) { const b = el('button', label, 'sm-add'); b.onclick = guardCall(onclick); return b; }
  function removeButton(onclick) { const b = el('button', '✕', 'sm-remove'); b.title = '移除'; b.onclick = guardCall(onclick); return b; }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { step, initialRuntime, clipSequence, frameAt };
  }
})();
