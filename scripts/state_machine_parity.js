#!/usr/bin/env node
/* JS/Python parity check for the action state machine engine.
   Usage: node scripts/state_machine_parity.js
   Builds an idle/walk/attack fixture, runs the same script through both the
   browser engine (static/state_machine.js) and the Flask backend engine,
   and asserts identical virtual-clock state, frame and reason traces. */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const engine = require(path.join(ROOT, 'static', 'state_machine.js'));

const ids = Array.from({ length: 12 }, () => Array.from({ length: 32 }, () =>
  Math.floor(Math.random() * 16).toString(16)).join(''));
const frames = ids.map((id, i) => ({ id, duration: 90, name: 'f' + (i + 1) }));
const clips = [
  { id: '11111111111111111111111111111111', name: '待机', frame_ids: ids.slice(0, 4), direction: 'forward', repeat: 0 },
  { id: '22222222222222222222222222222222', name: '行走', frame_ids: ids.slice(4, 8), direction: 'forward', repeat: 0 },
  { id: '33333333333333333333333333333333', name: '攻击', frame_ids: ids.slice(8, 12), direction: 'forward', repeat: 1 },
];
const project = { frames, clips };
const hex32 = n => String(n).repeat(32).slice(0, 32);
const [idle, walk, attack] = [hex32('a'), hex32('b'), hex32('c')];
const machine = {
  name: 'parity',
  events: [
    { id: 'move', name: '移动', key: 'Shift' },
    { id: 'attack', name: '攻击', key: 'Space' },
  ],
  states: [
    { id: idle, name: '待机', clip_id: clips[0].id, mode: 'loop', uninterruptible: 0 },
    { id: walk, name: '行走', clip_id: clips[1].id, mode: 'loop', uninterruptible: 0 },
    { id: attack, name: '攻击', clip_id: clips[2].id, mode: 'oneshot', uninterruptible: 120, fallback_state_id: idle },
  ],
  transitions: [
    { id: hex32('1'), from_state_id: idle, to_state_id: walk, event_id: 'move', phase: 'press', priority: 10, buffer_window: 200 },
    { id: hex32('2'), from_state_id: walk, to_state_id: idle, event_id: 'move', phase: 'release', priority: 10, buffer_window: 0 },
    { id: hex32('3'), from_state_id: idle, to_state_id: attack, event_id: 'attack', phase: 'press', priority: 20, buffer_window: 300 },
    { id: hex32('4'), from_state_id: walk, to_state_id: attack, event_id: 'attack', phase: 'press', priority: 20, buffer_window: 300 },
    { id: hex32('5'), from_state_id: attack, to_state_id: attack, event_id: 'attack', phase: 'press', priority: 20, buffer_window: 400 },
  ],
  initial_state_id: idle,
};
const events = [
  { at_ms: 100, event_id: 'attack', phase: 'press' },
  { at_ms: 200, event_id: 'attack', phase: 'press' },
  { at_ms: 900, event_id: 'move', phase: 'press' },
  { at_ms: 1300, event_id: 'move', phase: 'release' },
  { at_ms: 1500, event_id: 'attack', phase: 'press' },
  { at_ms: 1520, event_id: 'attack', phase: 'release' },
];

// JS engine: feed events one at a time, mirroring the live audition loop.
let jsRuntime = engine.initialRuntime(machine);
let previous = 0;
for (const event of events) {
  jsRuntime = engine.step(machine, jsRuntime, project, event.at_ms - previous,
    [{ at_ms: event.at_ms - previous, event_id: event.event_id, phase: event.phase }]);
  previous = event.at_ms;
}
jsRuntime = engine.step(machine, jsRuntime, project, 600, []);

// Python engine through the same stepping protocol.
const driver = `
import json, sys
sys.path.insert(0, ${JSON.stringify(ROOT)})
from flipbook.statemachine import step, initial_runtime
payload = json.load(sys.stdin)
machine, project, events = payload['machine'], payload['project'], payload['events']
runtime = initial_runtime(machine)
previous = 0
for event in events:
    runtime = step(machine, runtime, project, event['at_ms'] - previous,
                   [{'at_ms': event['at_ms'] - previous, 'event_id': event['event_id'], 'phase': event['phase']}])
    previous = event['at_ms']
runtime = step(machine, runtime, project, 600, [])
print(json.dumps(runtime, ensure_ascii=False))
`;
const py = spawnSync(process.env.PYTHON || 'python3', ['-I', '-c', driver], {
  input: JSON.stringify({ machine, project, events }), encoding: 'utf8',
});
if (py.status !== 0) {
  console.error(py.stderr);
  process.exit(1);
}
const pyRuntime = JSON.parse(py.stdout);
const slim = rt => ({
  time_ms: rt.time_ms, state_id: rt.state_id, frame_id: rt.frame_id,
  buffers: rt.buffers, held: rt.held,
  history: rt.history.map(h => ({ time_ms: h.time_ms, code: h.code, frame_id: h.frame_id || null,
    to_state_id: h.to_state_id || null, event_id: h.event_id || null, reason: h.reason || '' })),
});
const a = slim(jsRuntime), b = slim(pyRuntime);
assert.deepStrictEqual(a, b);
console.log('JS/Python engine parity OK:', a.history.length, 'reason traces, final time', a.time_ms, 'ms, state', a.state_id.slice(0, 8));
