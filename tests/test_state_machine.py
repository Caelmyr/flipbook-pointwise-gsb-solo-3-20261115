import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flipbook import create_app
from flipbook.model import new_id
from flipbook.statemachine import (
    validate_machine, simulate, initial_runtime, clip_sequence, example_machine,
)


class StateMachineWorkflows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = create_app(cls.temp.name)
        cls.app.testing = True
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions['jobs'].executor.shutdown(wait=True)
        cls.temp.cleanup()

    def call(self, method, path, data=None, expected=200):
        response = self.client.open(path, method=method, json=data)
        self.assertEqual(response.status_code, expected, response.json if response.is_json else response.data[:200])
        result = response.json if response.is_json else response.data
        response.close()
        return result

    def base(self, project):
        return '/api/projects/' + project['id']

    def revision(self, project_id):
        return self.call('GET', '/api/projects/' + project_id)['revision']

    def save_clip(self, project, name, frame_ids, repeat=0):
        return self.call('POST', self.base(project) + '/clips', {
            'revision': self.revision(project['id']), 'name': name,
            'frame_ids': frame_ids, 'direction': 'forward', 'repeat': repeat})

    def robot_project(self):
        project = self.call('POST', '/api/demo', {'kind': 'robot'}, 201)
        ids = [frame['id'] for frame in project['frames']]
        idle = self.save_clip(project, '待机片段', ids[0:4])['clips'][-1]
        walk = self.save_clip(project, '行走片段', ids[4:8])['clips'][-1]
        attack = self.save_clip(project, '攻击片段', ids[8:12], repeat=1)['clips'][-1]
        return self.call('GET', self.base(project)), idle, walk, attack

    def save_machine(self, project, machine, identifier=None, expected=200):
        path = self.base(project) + '/state-machines' + ('/' + identifier if identifier else '')
        payload = {'revision': self.revision(project['id']), 'machine': machine}
        if expected == 200:
            return self.call('PUT', path, payload, 200)
        return self.call('PUT', path, payload, expected)

    def test_example_build_and_full_audition_reasons(self):
        project, idle, walk, attack = self.robot_project()
        sample = self.call('POST', self.base(project) + '/state-machines/example', {})
        self.assertEqual([state['name'] for state in sample['states']], ['待机', '行走', '攻击'])
        self.assertEqual(sample['states'][2]['uninterruptible'], 120)
        # A long self-loop buffer makes the second attack re-fire after the one-shot.
        for transition in sample['transitions']:
            if transition['from_state_id'] == transition['to_state_id']:
                transition['buffer_window'] = 400
        saved = self.save_machine(project, sample, expected=201)
        machine = saved['machine']
        mid = machine['id']
        listing = self.call('GET', self.base(project) + '/state-machines')
        self.assertEqual([item['id'] for item in listing['machines']], [mid])
        attack_schedule = listing['schedules'][0]['states'][machine['states'][2]['id']]
        self.assertEqual(attack_schedule['total_duration'], 360)
        self.assertEqual(attack_schedule['frames'][1]['start'], 90)

        script = {'events': [
            {'at_ms': 100, 'event_id': 'attack', 'phase': 'press'},
            {'at_ms': 200, 'event_id': 'attack', 'phase': 'press'},
            {'at_ms': 1000, 'event_id': 'move', 'phase': 'press'},
            {'at_ms': 1300, 'event_id': 'move', 'phase': 'release'},
        ], 'tail_ms': 400}
        result = self.call('POST', f'{self.base(project)}/state-machines/{mid}/simulate', script)
        runtime = result['runtime']
        codes = [entry['code'] for entry in runtime['history']]
        self.assertIn('transition_blocked', codes)
        blocked = next(entry for entry in runtime['history'] if '不可打断' in entry.get('reason', ''))
        self.assertEqual(blocked['time_ms'], 200)
        completes = [entry for entry in runtime['history'] if entry['code'] == 'oneshot_complete']
        self.assertEqual(len(completes), 2)
        self.assertEqual(completes[0]['time_ms'], 460)
        fallbacks = [entry for entry in runtime['history'] if entry['code'] == 'fallback']
        self.assertTrue(all(entry['time_ms'] in (460, 820) for entry in fallbacks))
        walk_state = next(state for state in machine['states'] if state['name'] == '行走')
        idle_state = next(state for state in machine['states'] if state['name'] == '待机')
        self.assertEqual(runtime['state_id'], idle_state['id'])
        walk_entries = [entry for entry in runtime['history']
                        if entry['code'] == 'state_enter' and entry.get('to_state_id') == walk_state['id']]
        self.assertTrue(walk_entries)
        self.assertTrue(any('输入“move”触发' in entry['reason'] for entry in walk_entries))
        release = next(entry for entry in runtime['history'] if entry['code'] == 'input_release')
        self.assertEqual(release['event_id'], 'move')
        self.assertTrue(result['frame_image'].endswith(runtime['frame_id'] + '/image'))

    def test_uninterruptible_buffer_then_retroactively_fires(self):
        project, idle, walk, attack = self.robot_project()
        sample = self.call('POST', self.base(project) + '/state-machines/example', {})
        # Make the attack self-loop buffer long enough to survive the full one-shot.
        for transition in sample['transitions']:
            if transition['from_state_id'] == transition['to_state_id']:
                transition['buffer_window'] = 400
        saved = self.save_machine(project, sample, expected=201)
        mid, machine = saved['machine']['id'], saved['machine']
        result = self.call('POST', f'{self.base(project)}/state-machines/{mid}/simulate', {
            'events': [
                {'at_ms': 100, 'event_id': 'attack', 'phase': 'press'},
                {'at_ms': 200, 'event_id': 'attack', 'phase': 'press'},
            ], 'tail_ms': 600})
        attack_state = next(state for state in machine['states'] if state['name'] == '攻击')
        enters = [entry for entry in result['runtime']['history']
                  if entry['code'] == 'state_enter' and entry.get('to_state_id') == attack_state['id']]
        self.assertEqual(len(enters), 2)
        self.assertEqual(enters[1]['time_ms'], 460)
        self.assertTrue(any('不可打断' in entry.get('reason', '') for entry in result['runtime']['history']))

    def test_buffer_expiry_is_deterministic(self):
        project, idle, walk, attack = self.robot_project()
        s1 = new_id()
        machine = {
            'name': '暂存过期',
            'events': [{'id': 'attack', 'name': '攻击', 'key': 'Space'}],
            'states': [
                {'id': s1, 'name': '待机', 'clip_id': idle['id'], 'mode': 'loop', 'uninterruptible': 200},
            ],
            'transitions': [
                {'id': new_id(), 'from_state_id': s1, 'to_state_id': s1, 'event_id': 'attack',
                 'phase': 'press', 'priority': 10, 'buffer_window': 50},
            ],
            'initial_state_id': s1,
        }
        saved = self.save_machine(project, machine, expected=201)
        mid = saved['machine']['id']
        result = self.call('POST', f'{self.base(project)}/state-machines/{mid}/simulate', {
            'events': [{'at_ms': 0, 'event_id': 'attack', 'phase': 'press'}], 'tail_ms': 400})
        history = result['runtime']['history']
        expired = [entry for entry in history if entry['code'] == 'buffer_expired']
        self.assertTrue(expired)
        self.assertEqual(expired[0]['time_ms'], 400)
        self.assertEqual(result['runtime']['state_id'], s1)

    def test_priority_and_tie_break_are_deterministic(self):
        project, idle, walk, attack = self.robot_project()
        s0, s1, s2 = new_id(), new_id(), new_id()
        def build(priorities):
            return {
                'name': '优先级',
                'events': [{'id': 'go', 'name': '触发', 'key': 'Space'}],
                'states': [
                    {'id': s0, 'name': '待机', 'clip_id': idle['id'], 'mode': 'loop', 'uninterruptible': 0},
                    {'id': s1, 'name': '行走', 'clip_id': walk['id'], 'mode': 'loop', 'uninterruptible': 0},
                    {'id': s2, 'name': '攻击', 'clip_id': attack['id'], 'mode': 'oneshot',
                     'uninterruptible': 0, 'fallback_state_id': s0},
                ],
                'transitions': [
                    {'id': new_id(), 'from_state_id': s0, 'to_state_id': s1, 'event_id': 'go',
                     'phase': 'press', 'priority': priorities[0], 'buffer_window': 100},
                    {'id': new_id(), 'from_state_id': s0, 'to_state_id': s2, 'event_id': 'go',
                     'phase': 'press', 'priority': priorities[1], 'buffer_window': 100},
                ],
                'initial_state_id': s0,
            }
        high = self.save_machine(project, build([5, 10]), expected=201)['machine']
        runtime = simulate(high, self.call('GET', self.base(project)),
                           {'events': [{'at_ms': 0, 'event_id': 'go', 'phase': 'press'}]})
        self.assertEqual(runtime['state_id'], s2)
        self.assertTrue(any('同刻多条件' in entry.get('reason', '') for entry in runtime['history']))
        tied = self.save_machine(project, build([10, 10]), expected=201)['machine']
        runtime_tied = simulate(tied, self.call('GET', self.base(project)),
                                {'events': [{'at_ms': 0, 'event_id': 'go', 'phase': 'press'}]})
        self.assertEqual(runtime_tied['state_id'], s1)

    def test_real_exposure_frame_timing_and_fallback(self):
        project = self.call('POST', '/api/projects', {'name': '曝光时序', 'width': 32, 'height': 32}, 201)
        for _ in range(2):
            project = self.call('POST', self.base(project) + '/frames',
                                {'revision': project['revision'], 'count': 1})
        durations = [40, 60, 100]
        for frame, duration in zip(project['frames'], durations):
            project = self.call('PATCH', f"{self.base(project)}/frames/{frame['id']}",
                                {'revision': project['revision'], 'duration': duration})
        clip = self.save_clip(project, '一次性动作', [frame['id'] for frame in project['frames']], repeat=1)['clips'][-1]
        idle_clip = self.save_clip(project, '待机', [project['frames'][0]['id']])['clips'][-1]
        a, b = new_id(), new_id()
        machine = {
            'name': '曝光',
            'events': [], 'states': [
                {'id': a, 'name': '攻击', 'clip_id': clip['id'], 'mode': 'oneshot',
                 'uninterruptible': 30, 'fallback_state_id': b},
                {'id': b, 'name': '待机', 'clip_id': idle_clip['id'], 'mode': 'loop', 'uninterruptible': 0},
            ],
            'transitions': [], 'initial_state_id': a,
        }
        saved = self.save_machine(project, machine, expected=201)['machine']
        live = self.call('GET', self.base(project))
        runtime = simulate(saved, live, {'events': [], 'tail_ms': 250})
        frames = live['frames']
        changes = [(entry['time_ms'], entry['frame_id']) for entry in runtime['history']
                   if entry['code'] == 'frame_change' and entry['time_ms'] <= 200]
        self.assertEqual(changes, [(40, frames[0]['id']), (100, frames[1]['id']), (200, frames[2]['id'])])
        complete = next(entry for entry in runtime['history'] if entry['code'] == 'oneshot_complete')
        self.assertEqual(complete['time_ms'], 200)
        self.assertEqual(next(entry for entry in runtime['history'] if entry['code'] == 'fallback')['time_ms'], 200)

    def test_step_endpoint_matches_scripted_simulation(self):
        project, idle, walk, attack = self.robot_project()
        sample = self.call('POST', self.base(project) + '/state-machines/example', {})
        mid = self.save_machine(project, sample, expected=201)['machine']['id']
        events = [
            {'at_ms': 50, 'event_id': 'attack', 'phase': 'press'},
            {'at_ms': 300, 'event_id': 'move', 'phase': 'press'},
            {'at_ms': 700, 'event_id': 'move', 'phase': 'release'},
        ]
        whole = self.call('POST', f'{self.base(project)}/state-machines/{mid}/simulate',
                          {'events': events, 'tail_ms': 200})['runtime']
        runtime = None
        previous = 0
        for event in sorted(events, key=lambda item: item['at_ms']):
            payload = {'dt_ms': event['at_ms'] - previous,
                       'events': [{'at_ms': event['at_ms'] - previous,
                                   'event_id': event['event_id'], 'phase': event['phase']}]}
            if runtime:
                payload['runtime'] = runtime
            runtime = self.call('POST', f'{self.base(project)}/state-machines/{mid}/step', payload)['runtime']
            previous = event['at_ms']
        runtime = self.call('POST', f'{self.base(project)}/state-machines/{mid}/step',
                            {'runtime': runtime, 'dt_ms': 200, 'events': []})['runtime']
        self.assertEqual(runtime['state_id'], whole['state_id'])
        self.assertEqual(runtime['frame_id'], whole['frame_id'])
        self.assertEqual(runtime['time_ms'], whole['time_ms'])
        # Both execution paths share the same virtual-clock engine, so every
        # reason trace (timestamp, code and payload) must be identical.
        self.assertEqual(runtime['history'], whole['history'])

    def test_validation_rejects_broken_machine_and_blocks_clip_removal(self):
        project, idle, walk, attack = self.robot_project()
        s1, s2 = new_id(), new_id()
        bad = {
            'name': '坏状态机',
            'events': [{'id': 'attack', 'name': '攻击', 'key': 'Space'}],
            'states': [{'id': s1, 'name': '攻击', 'clip_id': attack['id'],
                        'mode': 'oneshot', 'uninterruptible': 0}],
            'transitions': [], 'initial_state_id': s1,
        }
        revision = self.revision(project['id'])
        self.save_machine(project, bad, expected=400)
        self.assertEqual(self.revision(project['id']), revision)
        good = {
            'name': '好状态机',
            'events': [{'id': 'attack', 'name': '攻击', 'key': 'Space'}],
            'states': [
                {'id': s1, 'name': '攻击', 'clip_id': attack['id'], 'mode': 'oneshot',
                 'uninterruptible': 0, 'fallback_state_id': s2},
                {'id': s2, 'name': '待机', 'clip_id': idle['id'], 'mode': 'loop', 'uninterruptible': 0},
            ],
            'transitions': [{'id': new_id(), 'from_state_id': s2, 'to_state_id': s1, 'event_id': 'attack',
                             'phase': 'press', 'priority': 10, 'buffer_window': 100}],
            'initial_state_id': s2,
        }
        saved = self.save_machine(project, good, expected=201)
        mid = saved['machine']['id']
        blocked = self.client.delete(self.base(project) + '/clips/' + attack['id'],
                                     json={'revision': self.revision(project['id'])})
        self.assertEqual(blocked.status_code, 400)
        blocked.close()
        project = self.call('DELETE', self.base(project) + '/state-machines/' + mid,
                            {'revision': self.revision(project['id'])})
        removed = self.client.delete(self.base(project) + '/clips/' + attack['id'],
                                     json={'revision': self.revision(project['id'])})
        self.assertEqual(removed.status_code, 200)
        removed.close()

    def test_machine_persists_across_app_restart(self):
        project, idle, walk, attack = self.robot_project()
        sample = self.call('POST', self.base(project) + '/state-machines/example', {})
        saved = self.save_machine(project, sample, expected=201)
        mid = saved['machine']['id']
        reloaded = create_app(self.temp.name)
        try:
            client = reloaded.test_client()
            listing = client.get(self.base(project) + '/state-machines')
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(listing.get_json()['machines'][0]['id'], mid)
            replay = client.post(f'{self.base(project)}/state-machines/{mid}/simulate', json={
                'events': [{'at_ms': 10, 'event_id': 'attack', 'phase': 'press'}], 'tail_ms': 800})
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.get_json()['runtime']['history'])
        finally:
            reloaded.extensions['jobs'].executor.shutdown(wait=True)

    def test_undo_redo_and_project_bundle_keep_machine(self):
        import io
        import zipfile
        from flipbook.exporting import project_bundle
        project, idle, walk, attack = self.robot_project()
        sample = self.call('POST', self.base(project) + '/state-machines/example', {})
        saved = self.save_machine(project, sample, expected=201)
        mid = saved['machine']['id']
        revision = saved['project']['revision']
        undone = self.call('POST', self.base(project) + '/undo', {'revision': revision})
        self.assertFalse(any(item['id'] == mid for item in undone.get('state_machines', [])))
        redone = self.call('POST', self.base(project) + '/redo', {'revision': undone['revision']})
        self.assertTrue(any(item['id'] == mid for item in redone['state_machines']))

        store = self.app.extensions['store']
        bundle = project_bundle(store, redone)
        response = self.client.post('/api/import-bundle',
                                    data={'file': (io.BytesIO(bundle), 'project.zip')})
        self.assertEqual(response.status_code, 201, response.json)
        copied = response.json
        self.assertEqual([item['name'] for item in copied['state_machines']],
                         [item['name'] for item in redone['state_machines']])
        copied_machine = copied['state_machines'][0]
        runtime = self.call('POST', f"{self.base(copied)}/state-machines/{copied_machine['id']}/simulate",
                            {'events': [], 'tail_ms': 100})
        self.assertEqual(runtime['runtime']['state_id'], copied_machine['initial_state_id'])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            document = json.loads(archive.read('project.json'))
        self.assertIn('state_machines', document)
        self.assertEqual(document['state_machines'][0]['id'], mid)

    def test_engine_rejects_unknown_input_gracefully(self):
        project, idle, walk, attack = self.robot_project()
        sample = example_machine(project)
        checked, _ = validate_machine(sample)
        live = self.call('GET', self.base(project))
        runtime = simulate(checked, live, {'events': [
            {'at_ms': 20, 'event_id': 'mystery', 'phase': 'press'},
            {'at_ms': 40, 'event_id': 'attack', 'phase': 'press'},
        ], 'tail_ms': 100})
        self.assertTrue(any(entry['code'] == 'unknown_event' for entry in runtime['history']))
        attack_state = next(state for state in checked['states'] if state['name'] == '攻击')
        self.assertEqual(runtime['state_id'], attack_state['id'])
        self.assertEqual(clip_sequence(live, attack['id'])[0]['frame_id'], attack['frame_ids'][0])
        blank = initial_runtime(checked)
        self.assertEqual(blank['buffers'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
