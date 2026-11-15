import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flipbook import create_app


class PlaytestWorkflows(unittest.TestCase):
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

    def level(self, columns=6, rows=3):
        project = self.call('POST', '/api/projects', {'name': '机关试验', 'width': 32, 'height': 32}, 201)
        project = self.call('POST', self.base(project) + '/maps', {
            'revision': project['revision'], 'columns': columns, 'rows': rows,
            'tile_width': 16, 'tile_height': 16, 'tiles': [project['frames'][0]['id']],
        })
        return project, project['tilemaps'][0]['id']

    def save(self, project, map_id, config, expected=200):
        return self.call('PUT', f"{self.base(project)}/maps/{map_id}/playtest",
                         {'revision': project['revision'], 'config': config}, expected)

    def simulate(self, project, map_id, moves, expected=200):
        return self.call('POST', f"{self.base(project)}/maps/{map_id}/playtest/simulate", {'moves': moves}, expected)

    def analyze(self, project, map_id, moves=None, extra=None, expected=200):
        payload = {'moves': moves or []}
        payload.update(extra or {})
        return self.call('POST', f"{self.base(project)}/maps/{map_id}/playtest/analyze", payload, expected)

    def softlock_level(self):
        project, map_id = self.level(6, 3)
        config = {
            'start': [0, 0], 'exit': [5, 0],
            'blocks': [[3, 1], [4, 1], [5, 1], [3, 2]],
            'keys': [{'kind': 'gold', 'at': [1, 1]}],
            'doors': [{'kind': 'gold', 'at': [2, 0]}, {'kind': 'gold', 'at': [2, 1]}],
        }
        project = self.save(project, map_id, config)
        return project, map_id

    def test_config_save_persist_undo_and_tiles_untouched(self):
        project, map_id = self.level()
        layers_before = project['tilemaps'][0]['layers']
        self.assertNotIn('playtest', project['tilemaps'][0])
        config = {'start': [0, 0], 'exit': [5, 2], 'blocks': [[2, 1]],
                  'keys': [{'kind': 'red', 'at': [1, 0]}],
                  'switches': [{'at': [3, 0], 'on': True}],
                  'oneways': [{'at': [4, 0], 'dir': 'right'}]}
        project = self.save(project, map_id, config)
        tilemap = project['tilemaps'][0]
        self.assertEqual(tilemap['layers'], layers_before)
        stored = tilemap['playtest']
        self.assertEqual(stored['start'], [0, 0])
        self.assertEqual(stored['blocks'], [[2, 1]])
        self.assertEqual(stored['keys'][0]['kind'], 'red')
        self.assertEqual(stored['switches'][0]['name'], '开关1')
        self.assertTrue(stored['switches'][0]['on'])
        switch_id = stored['switches'][0]['id']
        config2 = dict(stored, gates=[{'switch': switch_id, 'at': [5, 0]}])
        project = self.save(project, map_id, config2)
        self.assertEqual(project['tilemaps'][0]['playtest']['gates'][0]['switch'], switch_id)
        another = create_app(self.temp.name)
        try:
            reloaded = another.test_client().get(self.base(project)).json
            self.assertEqual(reloaded['tilemaps'][0]['playtest'], project['tilemaps'][0]['playtest'])
        finally:
            another.extensions['jobs'].executor.shutdown(wait=True)
        project = self.call('POST', self.base(project) + '/undo', {'revision': project['revision']})
        self.assertEqual(project['tilemaps'][0]['playtest']['gates'], [])
        project = self.call('POST', self.base(project) + '/undo', {'revision': project['revision']})
        self.assertNotIn('playtest', project['tilemaps'][0])
        project = self.call('POST', self.base(project) + '/redo', {'revision': project['revision']})
        self.assertIn('playtest', project['tilemaps'][0])

    def test_config_validation_errors(self):
        project, map_id = self.level()
        revision = project['revision']
        base = {'start': [0, 0], 'exit': [5, 2]}
        bad = [
            dict(base, start=[1, 1], exit=[1, 1]),
            dict(base, blocks=[[1, 1]], keys=[{'kind': 'gold', 'at': [1, 1]}]),
            dict(base, keys=[{'kind': 'gold', 'at': [2, 2]}], doors=[{'kind': 'gold', 'at': [2, 2]}]),
            dict(base, oneways=[{'at': [1, 1], 'dir': 'north'}]),
            dict(base, keys=[{'kind': 'pink', 'at': [1, 1]}]),
            dict(base, keys=[{'kind': 'gold', 'at': [i % 6, i // 6]} for i in range(17)]),
            dict(base, gates=[{'switch': '0' * 32, 'at': [1, 1]}]),
            dict(base, start=[2, 2], doors=[{'kind': 'gold', 'at': [2, 2]}]),
            dict(base, exit=[3, 3], keys=[{'kind': 'gold', 'at': [3, 3]}]),
            dict(base, blocks=[[0, 0]]),
        ]
        for config in bad:
            self.save(project, map_id, config, 400)
        self.assertEqual(self.client.get(self.base(project)).json['revision'], revision)

    def test_simulate_requires_start_and_exit(self):
        project, map_id = self.level()
        project = self.save(project, map_id, {'start': [0, 0]})
        self.simulate(project, map_id, [], 400)

    def test_key_pickup_once_and_door_consumes(self):
        project, map_id = self.level(5, 1)
        config = {'start': [0, 0], 'exit': [4, 0],
                  'keys': [{'kind': 'gold', 'at': [1, 0]}],
                  'doors': [{'kind': 'gold', 'at': [2, 0]}, {'kind': 'gold', 'at': [3, 0]}]}
        project = self.save(project, map_id, config)
        result = self.simulate(project, map_id, ['right', 'left', 'right'])
        pickups = [event for events in result['events'] for event in events if '拾取' in event]
        self.assertEqual(len(pickups), 1)
        self.assertEqual(result['states'][-1]['inventory'], {'gold': 1})
        result = self.simulate(project, map_id, ['right', 'right', 'right'])
        self.assertEqual(result['applied'], 2)
        self.assertEqual(result['rejected']['index'], 2)
        self.assertIn('需要', result['rejected']['reason'])
        self.assertEqual(result['states'][-1]['inventory'], {})
        self.assertEqual(len(result['states'][-1]['open_doors']), 1)

    def test_switch_toggles_gate_and_retoggle(self):
        project, map_id = self.level(4, 2)
        config = {'start': [0, 0], 'exit': [3, 0], 'switches': [{'at': [1, 1]}]}
        project = self.save(project, map_id, config)
        stored = project['tilemaps'][0]['playtest']
        switch_id = stored['switches'][0]['id']
        project = self.save(project, map_id, dict(stored, gates=[{'switch': switch_id, 'at': [2, 0]}]))
        result = self.simulate(project, map_id, ['right', 'right'])
        self.assertEqual(result['applied'], 1)
        self.assertIn('闸门', result['rejected']['reason'])
        result = self.simulate(project, map_id, ['down', 'right', 'up', 'right', 'right'])
        self.assertTrue(result['won'])
        result = self.simulate(project, map_id, ['down', 'right', 'left', 'right'])
        self.assertFalse(result['states'][-1]['switches'][switch_id])

    def test_oneway_restricts_both_directions(self):
        project, map_id = self.level(3, 1)
        config = {'start': [0, 0], 'exit': [2, 0], 'oneways': [{'at': [1, 0], 'dir': 'right'}]}
        project = self.save(project, map_id, config)
        self.assertTrue(self.simulate(project, map_id, ['right', 'right'])['won'])
        project2, map2 = self.level(3, 1)
        config2 = {'start': [2, 0], 'exit': [0, 0], 'oneways': [{'at': [1, 0], 'dir': 'right'}]}
        project2 = self.save(project2, map2, config2)
        result = self.simulate(project2, map2, ['left'])
        self.assertEqual(result['applied'], 0)
        self.assertIn('单向', result['rejected']['reason'])
        project3, map3 = self.level(3, 1)
        config3 = {'start': [1, 0], 'exit': [2, 0], 'oneways': [{'at': [1, 0], 'dir': 'right'}]}
        project3 = self.save(project3, map3, config3)
        result = self.simulate(project3, map3, ['left'])
        self.assertEqual(result['applied'], 0)
        self.assertIn('单向', result['rejected']['reason'])

    def test_blocks_bounds_and_broken_history(self):
        project, map_id = self.level(3, 2)
        config = {'start': [0, 0], 'exit': [2, 1], 'blocks': [[1, 0]]}
        project = self.save(project, map_id, config)
        result = self.simulate(project, map_id, ['right'])
        self.assertEqual(result['applied'], 0)
        self.assertIn('阻挡', result['rejected']['reason'])
        result = self.simulate(project, map_id, ['up'])
        self.assertIn('边界', result['rejected']['reason'])
        result = self.simulate(project, map_id, ['down', 'right', 'right', 'right', 'left'])
        self.assertEqual(result['applied'], 3)
        self.assertTrue(result['won'])
        self.assertEqual(result['rejected']['index'], 3)

    def test_route_replay_matches_manual_rules(self):
        project, map_id = self.softlock_level()
        analysis = self.analyze(project, map_id)
        route = analysis['route']
        self.assertEqual(route['status'], 'found')
        self.assertEqual(len(route['positions']), route['steps'] + 1)
        replayed = self.simulate(project, map_id, route['moves'])
        self.assertTrue(replayed['won'])
        self.assertEqual(replayed['applied'], route['steps'])
        for count in range(1, route['steps'] + 1):
            prefix = self.simulate(project, map_id, route['moves'][:count])
            self.assertEqual(prefix['applied'], count)
            self.assertEqual(prefix['states'][-1]['pos'], route['positions'][count])

    def test_unreachable_vs_softlock_and_choices_on_map(self):
        project, map_id = self.softlock_level()
        analysis = self.analyze(project, map_id)
        level = analysis['level']
        self.assertTrue(level['complete'])
        self.assertTrue(level['solvable'])
        self.assertEqual(sorted(level['unreachable_cells']), [[4, 2], [5, 2]])
        self.assertGreater(level['softlock_states'], 0)
        choices = level['softlock_transitions']
        self.assertEqual(level['softlock_transition_count'], len(choices))
        self.assertIn({'from': [1, 1], 'direction': 'right', 'to': [2, 1]}, choices)
        self.assertIn([2, 1], level['doomed_cells'])
        self.assertIn([1, 1], level['doomed_cells'])
        self.assertGreater(level['reachable_states'], 6 - len(level['unreachable_cells']))
        doomed = self.analyze(project, map_id, ['down', 'right', 'right'])
        self.assertEqual(doomed['current']['pos'], [2, 1])
        self.assertEqual(doomed['route']['status'], 'impossible')
        self.assertTrue(doomed['level']['solvable'])

    def test_same_cell_different_state_not_merged(self):
        project, map_id = self.level(4, 1)
        config = {'start': [0, 0], 'exit': [3, 0],
                  'keys': [{'kind': 'gold', 'at': [1, 0]}],
                  'doors': [{'kind': 'gold', 'at': [2, 0]}]}
        project = self.save(project, map_id, config)
        analysis = self.analyze(project, map_id)
        level = analysis['level']
        self.assertTrue(level['solvable'])
        cells = 4
        self.assertGreater(level['reachable_states'], cells)
        project2, map2 = self.level(4, 1)
        config2 = {'start': [0, 0], 'exit': [3, 0], 'doors': [{'kind': 'gold', 'at': [2, 0]}]}
        project2 = self.save(project2, map2, config2)
        analysis2 = self.analyze(project2, map2)
        self.assertFalse(analysis2['level']['solvable'])
        self.assertEqual(analysis2['route']['status'], 'impossible')

    def test_incomplete_analysis_not_reported_as_unsolvable(self):
        project, map_id = self.level(10, 10)
        config = {'start': [0, 0], 'exit': [9, 9],
                  'switches': [{'at': [i, 0]} for i in range(1, 9)]}
        project = self.save(project, map_id, config)
        analysis = self.analyze(project, map_id, extra={'max_states': 1000})
        self.assertFalse(analysis['level']['complete'])
        self.assertIsNone(analysis['level']['solvable'])
        self.assertIsNone(analysis['level']['unreachable_cells'])
        self.assertIn('尚未完成', analysis['level']['note'])
        self.assertEqual(analysis['route']['status'], 'unknown')
        self.assertIn('尚未完成', analysis['route']['note'])
        full = self.analyze(project, map_id)
        self.assertTrue(full['level']['complete'])
        self.assertTrue(full['level']['solvable'])

    def test_fingerprint_tracks_config_and_undo(self):
        project, map_id = self.softlock_level()
        first = self.simulate(project, map_id, [])['fingerprint']
        config = project['tilemaps'][0]['playtest']
        modified = dict(config, blocks=config['blocks'] + [[0, 2]])
        project = self.save(project, map_id, modified)
        second = self.simulate(project, map_id, [])['fingerprint']
        self.assertNotEqual(first, second)
        project = self.call('POST', self.base(project) + '/undo', {'revision': project['revision']})
        third = self.simulate(project, map_id, [])['fingerprint']
        self.assertEqual(first, third)

    def test_moves_from_won_state_rejected(self):
        project, map_id = self.level(2, 1)
        project = self.save(project, map_id, {'start': [0, 0], 'exit': [1, 0]})
        result = self.simulate(project, map_id, ['right', 'left'])
        self.assertEqual(result['applied'], 1)
        self.assertTrue(result['won'])
        self.assertIn('出口', result['rejected']['reason'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
