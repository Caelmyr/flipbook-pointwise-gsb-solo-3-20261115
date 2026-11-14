import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flipbook import create_app
from flipbook.composite import png_bytes, render_frame, blend_pixels
from flipbook.drawing import draw
from flipbook.validate import draw_command
from flipbook.tween import premultiplied_crossfade, ease, bezier_position
from flipbook.atlas import build_atlas
from flipbook.atlas_import import unpack_sprite, validate_atlas
from flipbook.ninepatch import nine_slice, seam_blend
from flipbook.exporting import animated_gif, animated_png, project_bundle


class Workflows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = create_app(cls.temp.name)
        cls.app.testing = True
        cls.client = cls.app.test_client()
        cls.store = cls.app.extensions['store']

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

    def new(self, width=32, height=32):
        return self.call('POST', '/api/projects', {'name': '测试动画', 'width': width, 'height': height}, 201)

    def demo(self, kind='robot'):
        return self.call('POST', '/api/demo', {'kind': kind}, 201)

    def base(self, project):
        return '/api/projects/' + project['id']

    def mutate(self, project, path, data=None, method='POST'):
        payload = {'revision': project['revision']}
        payload.update(data or {})
        return self.call(method, self.base(project) + '/' + path, payload)

    def layer_path(self, project, suffix=''):
        frame = project['frames'][0]
        return 'frames/' + frame['id'] + '/layers/' + frame['layers'][0]['id'] + ('/' + suffix if suffix else '')

    def paint(self, project, color='#ff0000ff', points=None):
        return self.mutate(project, self.layer_path(project, 'draw'), {
            'tool': 'rectangle', 'color': color,
            'points': points or [[4, 4], [12, 12]], 'filled': True,
        })

    def image(self, project, frame=0):
        return render_frame(self.store, project, project['frames'][frame]['id'])

    def test_all_original_demos(self):
        for kind, count in [('robot', 12), ('flame', 12), ('orb', 12), ('tiles', 3)]:
            project = self.demo(kind)
            self.assertEqual(len(project['frames']), count)
            self.assertIsNotNone(self.image(project).getbbox())
            self.assertTrue(self.call('GET', self.base(project) + '/integrity')['valid'])

    def test_pencil_draw_and_undo_redo(self):
        project = self.new()
        blank = np.asarray(self.image(project)).copy()
        project = self.mutate(project, self.layer_path(project, 'draw'), {'tool': 'pencil', 'color': '#ff0000ff', 'points': [[3, 3], [10, 3]], 'size': 1})
        self.assertEqual(self.image(project).getpixel((6, 3)), (255, 0, 0, 255))
        project = self.mutate(project, 'undo')
        np.testing.assert_array_equal(np.asarray(self.image(project)), blank)
        project = self.mutate(project, 'redo')
        self.assertEqual(self.image(project).getpixel((6, 3)), (255, 0, 0, 255))

    def test_mirror_drawing_and_erase(self):
        project = self.new()
        project = self.mutate(project, self.layer_path(project, 'draw'), {'tool': 'pencil', 'points': [[3, 4]], 'color': '#00ff00ff', 'mirror_x': True})
        image = self.image(project)
        self.assertEqual(image.getpixel((3, 4)), (0, 255, 0, 255))
        self.assertEqual(image.getpixel((28, 4)), (0, 255, 0, 255))
        project = self.mutate(project, self.layer_path(project, 'draw'), {'tool': 'erase', 'points': [[3, 4]]})
        self.assertEqual(self.image(project).getpixel((3, 4))[3], 0)

    def test_flood_fill_stays_inside_shape(self):
        project = self.new()
        project = self.mutate(project, self.layer_path(project, 'draw'), {'tool': 'rectangle', 'points': [[4, 4], [20, 20]], 'color': '#ffffffff', 'filled': False})
        project = self.mutate(project, self.layer_path(project, 'draw'), {'tool': 'fill', 'points': [[10, 10]], 'color': '#ff0000ff'})
        self.assertEqual(self.image(project).getpixel((10, 10)), (255, 0, 0, 255))
        self.assertEqual(self.image(project).getpixel((0, 0))[3], 0)

    def test_layer_opacity_and_blending(self):
        first = Image.new('RGBA', (1, 1), (255, 0, 0, 255))
        second = Image.new('RGBA', (1, 1), (0, 0, 255, 128))
        output = blend_pixels(first, second)
        r, g, b, a = output.getpixel((0, 0))
        self.assertEqual(a, 255)
        self.assertAlmostEqual(r, 127, delta=1)
        self.assertAlmostEqual(b, 128, delta=1)
        multiply = blend_pixels(first, Image.new('RGBA', (1, 1), (0, 255, 0, 255)), 'multiply')
        self.assertEqual(multiply.getpixel((0, 0)), (0, 0, 0, 255))

    def test_locked_layer_rejects_and_revision_unchanged(self):
        project = self.new()
        project = self.mutate(project, self.layer_path(project), {'locked': True}, 'PATCH')
        response = self.client.post(self.base(project) + '/' + self.layer_path(project, 'draw'), json={'revision': project['revision'], 'tool': 'pencil', 'points': [[2, 2]]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])

    def test_layer_duplicate_merge_matches_render(self):
        project = self.paint(self.new())
        project = self.mutate(project, self.layer_path(project, 'duplicate'))
        before = np.asarray(self.image(project)).copy()
        top = project['frames'][0]['layers'][-1]['id']
        project = self.mutate(project, 'frames/' + project['frames'][0]['id'] + '/layers/' + top + '/merge')
        self.assertEqual(len(project['frames'][0]['layers']), 1)
        np.testing.assert_array_equal(before, np.asarray(self.image(project)))

    def test_retime_preserves_exact_total(self):
        project = self.demo()
        project = self.mutate(project, 'retime', {'mode': 'total', 'total': 1001})
        self.assertEqual(sum(f['duration'] for f in project['frames']), 1001)
        self.assertTrue(all(20 <= f['duration'] <= 10000 for f in project['frames']))
        reversed_ids = [f['id'] for f in project['frames']][::-1]
        project = self.mutate(project, 'reverse')
        self.assertEqual([f['id'] for f in project['frames']], reversed_ids)

    def test_pingpong_does_not_duplicate_endpoints(self):
        project = self.demo('tiles')
        project = self.mutate(project, 'pingpong')
        self.assertEqual(len(project['frames']), 4)
        self.assertEqual(self.image(project, 1).tobytes(), self.image(project, 3).tobytes())

    def test_transform_tween_end_state(self):
        project = self.paint(self.new())
        frame = project['frames'][0]
        project = self.mutate(project, 'tween/transform', {'frame_id': frame['id'], 'layer_id': frame['layers'][0]['id'], 'count': 4, 'target': {'x': 8, 'y': 0}, 'easing': 'linear'})
        self.assertEqual(len(project['frames']), 5)
        transform = project['frames'][-1]['layers'][0]['transform']
        self.assertEqual(transform['x'], 8)
        self.assertEqual(self.image(project, -1).getpixel((12, 4)), (255, 0, 0, 255))

    def test_crossfade_preserves_unassociated_color(self):
        first = Image.new('RGBA', (1, 1), (255, 0, 0, 255))
        second = Image.new('RGBA', (1, 1), (0, 0, 255, 0))
        output = premultiplied_crossfade(first, second, .5)
        self.assertEqual(output.getpixel((0, 0)), (255, 0, 0, 128))
        self.assertEqual(bezier_position([[0, 0], [10, 10]], .5), [5, 5])
        for mode in ['linear', 'ease_in', 'ease_out', 'ease_in_out', 'smoothstep', 'bounce', 'elastic', 'steps']:
            self.assertAlmostEqual(ease(0, mode), 0)
            self.assertAlmostEqual(ease(1, mode), 1)

    def test_palette_extraction_and_mapping(self):
        project = self.demo()
        result = self.call('POST', self.base(project) + '/palette/extract', {'count': 4})
        self.assertLessEqual(len(result['palette']), 4)
        before_alpha = np.asarray(self.image(project))[:, :, 3].copy()
        project = self.mutate(project, 'palette/apply', {'palette': ['#000000ff', '#ffffffff'], 'dither': 'ordered'})
        raster = np.asarray(self.image(project))
        colors = np.unique(raster[raster[:, :, 3] > 0][:, :3], axis=0)
        self.assertLessEqual(len(colors), 2)
        np.testing.assert_array_equal(before_alpha, raster[:, :, 3])

    def test_geometry_scales_boxes_and_pivots(self):
        project = self.demo()
        project = self.mutate(project, 'resize', {'mode': 'scale', 'width': 128, 'height': 128})
        self.assertEqual(project['frames'][0]['boxes'][0]['width'], 56)
        self.assertEqual(self.image(project).size, (128, 128))
        project = self.mutate(project, 'rotate', {'turns': 1})
        self.assertEqual(project['frames'][0]['boxes'], [])
        self.assertAlmostEqual(project['frames'][0]['pivot'][0], .15)

    def test_atlas_pixel_roundtrip_and_no_overlaps(self):
        project = self.demo()
        image, metadata = build_atlas(self.store, project, {'trim': True, 'rotate': True, 'extrude': 1})
        entries = validate_atlas(metadata, image.size)
        for entry in entries:
            restored = unpack_sprite(image, entry)
            expected = render_frame(self.store, project, entry['old_id'])
            np.testing.assert_array_equal(np.asarray(restored), np.asarray(expected))
        boxes = [entry['frame'] for entry in metadata['frames']]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                intersects = a['x'] < b['x'] + b['width'] and a['x'] + a['width'] > b['x'] and a['y'] < b['y'] + b['height'] and a['y'] + a['height'] > b['y']
                self.assertFalse(intersects)

    def test_apng_and_gif_decode_frames_duration_transparency(self):
        project = self.demo()
        for encoder in [animated_png, animated_gif]:
            data, details = encoder(self.store, project, {})
            with Image.open(io.BytesIO(data)) as decoded:
                self.assertEqual(decoded.n_frames, 12)
                duration = 0
                for index in range(decoded.n_frames):
                    decoded.seek(index)
                    duration += decoded.info['duration']
                    self.assertEqual(decoded.convert('RGBA').getpixel((0, 0))[3], 0)
                self.assertEqual(duration, 1080)

    def test_alpha_bleed_survives_render_and_png_export(self):
        project = self.paint(self.new(), '#ff0000ff')
        frame = project['frames'][0]
        project = self.mutate(project, 'effect', {
            'frame_ids': [frame['id']],
            'layer_id': frame['layers'][0]['id'],
            'operation': 'alpha_bleed',
            'radius': 2,
        })
        rendered = self.image(project)
        self.assertEqual(rendered.getpixel((3, 6)), (255, 0, 0, 0))
        with Image.open(io.BytesIO(png_bytes(rendered))) as decoded:
            self.assertEqual(decoded.convert('RGBA').getpixel((3, 6)), (255, 0, 0, 0))
            self.assertEqual(decoded.convert('RGBA').getpixel((6, 6)), (255, 0, 0, 255))

    def test_gif_opaque_black_not_reserved_transparency(self):
        project = self.paint(self.new(), '#000000ff')
        data, _ = animated_gif(self.store, project, {})
        with Image.open(io.BytesIO(data)) as decoded:
            pixel = decoded.convert('RGBA').getpixel((6, 6))
            self.assertEqual(pixel, (0, 0, 0, 255))

    def test_project_bundle_roundtrip(self):
        project = self.demo()
        bundle = project_bundle(self.store, project)
        response = self.client.post('/api/import-bundle', data={'file': (io.BytesIO(bundle), 'project.zip')})
        self.assertEqual(response.status_code, 201, response.json)
        copied = response.json
        self.assertNotEqual(copied['id'], project['id'])
        self.assertEqual(len(copied['frames']), 12)
        np.testing.assert_array_equal(np.asarray(self.image(project)), np.asarray(self.image(copied)))

    def test_archive_rejects_traversal_and_missing_assets(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('../outside.txt', 'bad')
            archive.writestr('project.json', '{}')
        response = self.client.post('/api/import-bundle', data={'file': (io.BytesIO(stream.getvalue()), 'bad.zip')})
        self.assertEqual(response.status_code, 400)

    def test_tilemap_fill_paint_and_export(self):
        project = self.demo('tiles')
        project = self.mutate(project, 'maps', {'columns': 4, 'rows': 3, 'tile_width': 16, 'tile_height': 16})
        map_id = project['tilemaps'][0]['id']
        project = self.mutate(project, f'maps/{map_id}/fill', {'mode': 'checker', 'first': 1, 'second': 2})
        self.assertEqual(project['tilemaps'][0]['layers'][0]['data'][:4], [1, 2, 1, 2])
        project = self.mutate(project, f'maps/{map_id}/paint', {'tile': 3, 'cells': [[0, 0]], 'flip_x': True})
        self.assertEqual(project['tilemaps'][0]['layers'][0]['data'][0], 0x80000003)
        data = self.call('GET', self.base(project) + f'/maps/{map_id}/image')
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (64, 48))
        bundle = self.call('GET', self.base(project) + f'/maps/{map_id}/export')
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            metadata = json.loads(archive.read('map.tmj'))
            self.assertEqual(len(metadata['layers'][0]['data']), 12)
            self.assertIn('tiles/1.png', archive.namelist())

    def rule_project(self):
        project = self.demo('tiles')
        project = self.mutate(project, 'maps', {'columns': 5, 'rows': 4, 'tile_width': 16, 'tile_height': 16})
        map_id = project['tilemaps'][0]['id']
        rules = {'name': '地形接口', 'tiles': {
            '1': {'north': 'G', 'east': 'S', 'south': 'D', 'west': 'W'},
            '2': {'north': 'D', 'east': 'W', 'south': 'G', 'west': 'S'},
            '3': {'north': 'G', 'east': 'W', 'south': 'G', 'west': 'W'},
        }}
        project = self.mutate(project, f'maps/{map_id}/edge-rules', {'rules': rules})
        return project, map_id

    def rule_preview(self, project, map_id, data, expected=200):
        payload = {'revision': project['revision'], 'region': {'x': 0, 'y': 0, 'width': 3, 'height': 3}}
        payload.update(data)
        return self.call('POST', self.base(project) + f'/maps/{map_id}/edge-rules/preview', payload, expected)

    def test_rule_solve_reproducible_commit_and_single_undo(self):
        project, map_id = self.rule_project()
        before = json.dumps(project['tilemaps'][0]['layers'][0]['data'])
        first = self.rule_preview(project, map_id, {'seed': 7})
        second = self.rule_preview(project, map_id, {'seed': 7})
        other = self.rule_preview(project, map_id, {'seed': 99})
        self.assertEqual(first['status'], 'solved')
        self.assertEqual(first['stats']['assigned'], 9)
        self.assertEqual(first['stats']['undetermined'], 0)
        self.assertEqual(first['fingerprint'], second['fingerprint'])
        self.assertNotEqual(first['fingerprint'], other['fingerprint'])
        self.assertEqual(first['cells'], second['cells'])
        self.assertEqual(first['connections'], second['connections'])
        self.assertEqual(len(first['connections']), 12)
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])
        committed = self.mutate(project, f'maps/{map_id}/edge-rules/commit', {
            'region': first['region'], 'seed': 7, 'mode': 'solve', 'pins': [], 'fingerprint': first['fingerprint'],
        })
        layer = committed['tilemaps'][0]['layers'][0]
        expected = [0] * 20
        for cell in first['cells']:
            expected[cell['y'] * 5 + cell['x']] = cell['value']
        self.assertEqual(layer['data'], expected)
        undone = self.mutate(committed, 'undo')
        self.assertEqual(json.dumps(undone['tilemaps'][0]['layers'][0]['data']), before)

    def test_rule_conflict_names_pins_and_rules_and_refuses_commit(self):
        project, map_id = self.rule_project()
        before = json.dumps(project['tilemaps'][0]['layers'][0]['data'])
        conflict = self.rule_preview(project, map_id, {'pins': [{'x': 0, 'y': 0, 'tile': 1}, {'x': 0, 'y': 1, 'tile': 3}]})
        self.assertEqual(conflict['status'], 'conflict')
        self.assertIsNone(conflict['fingerprint'])
        self.assertEqual([(pin['x'], pin['y'], pin['tile']) for pin in conflict['conflicting_pins']], [(0, 0, 1), (0, 1, 3)])
        self.assertEqual([rule['tile'] for rule in conflict['conflicting_rules']], [1, 3])
        self.assertIn('瓦片1', conflict['message'])
        self.assertIn('瓦片3', conflict['message'])
        response = self.client.post(self.base(project) + f'/maps/{map_id}/edge-rules/commit', json={
            'revision': project['revision'], 'region': {'x': 0, 'y': 0, 'width': 3, 'height': 3},
            'pins': [{'x': 0, 'y': 0, 'tile': 1}, {'x': 0, 'y': 1, 'tile': 3}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('未修改地图', response.json['error'])
        actual = self.store.get(project['id'])
        self.assertEqual(actual['revision'], project['revision'])
        self.assertEqual(json.dumps(actual['tilemaps'][0]['layers'][0]['data']), before)
        solved = self.rule_preview(project, map_id, {'pins': [{'x': 0, 'y': 0, 'tile': 1}]})
        self.assertEqual(solved['status'], 'solved')
        self.assertEqual(solved['stats']['pinned'], 1)

    def test_rule_manual_key_tiles_can_be_relaxed_then_solved(self):
        project, map_id = self.rule_project()
        pins = [{'x': 0, 'y': 0, 'tile': 1}, {'x': 0, 'y': 1, 'tile': 3}]
        conflict = self.rule_preview(project, map_id, {'pins': pins})
        self.assertEqual(conflict['status'], 'conflict')
        relaxed = self.rule_preview(project, map_id, {'pins': pins[1:]})
        self.assertEqual(relaxed['status'], 'solved')
        self.assertEqual(relaxed['stats']['pinned'], 1)
        local = self.rule_preview(project, map_id, {
            'pins': pins, 'region': {'x': 0, 'y': 0, 'width': 3, 'height': 2},
        })
        self.assertEqual(local['status'], 'conflict')
        self.assertEqual([(pin['x'], pin['y']) for pin in local['conflicting_pins']], [(0, 0), (0, 1)])
        pinned_existing = self.mutate(project, f'maps/{map_id}/paint', {'tile': 2, 'cells': [[1, 1]]})
        kept = self.rule_preview(pinned_existing, map_id, {'pin_existing': True, 'region': {'x': 0, 'y': 0, 'width': 3, 'height': 3}})
        self.assertEqual(kept['status'], 'solved')
        self.assertEqual(kept['stats']['pinned'], 1)
        cell = next(item for item in kept['cells'] if (item['x'], item['y']) == (1, 1))
        self.assertEqual((cell['tile'], cell['status']), (2, 'pinned'))
        overridden = self.rule_preview(pinned_existing, map_id, {'pin_existing': False, 'region': {'x': 0, 'y': 0, 'width': 3, 'height': 3}, 'pins': [{'x': 1, 'y': 1, 'tile': 3}]})
        self.assertEqual(overridden['status'], 'solved')
        self.assertEqual(next(item for item in overridden['cells'] if (item['x'], item['y']) == (1, 1))['tile'], 3)

    def test_rule_border_tiles_actually_constrain_domain(self):
        project, map_id = self.rule_project()
        rules = {'name': '接缝', 'tiles': {
            '1': {'north': 'G', 'east': 'S', 'south': 'D', 'west': 'W'},
            '2': {'north': 'D', 'east': 'W', 'south': 'G', 'west': 'S'},
            '3': {'north': 'G', 'east': 'W', 'south': 'G', 'west': 'W'},
        }}
        project = self.mutate(project, f'maps/{map_id}/edge-rules', {'rules': rules})
        project = self.mutate(project, f'maps/{map_id}/paint', {'tile': 1, 'cells': [[0, 1]]})
        border_only = self.rule_preview(project, map_id, {'region': {'x': 1, 'y': 1, 'width': 1, 'height': 1}, 'mode': 'propagate'})
        self.assertEqual(border_only['stats']['undetermined'], 0)
        self.assertEqual(border_only['cells'][0]['tile'], 2)
        self.assertGreaterEqual(border_only['connection_count'], 1)
        self.assertTrue(any(item['to_border'] for item in border_only['connections']))
        conflicting_border = self.mutate(project, f'maps/{map_id}/paint', {'tile': 2, 'cells': [[2, 1]]})
        result = self.rule_preview(conflicting_border, map_id, {'region': {'x': 1, 'y': 1, 'width': 1, 'height': 1}})
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['conflicting_fixed'][0]['source'], '区域外边界')
        self.assertEqual((result['conflicting_fixed'][0]['x'], result['conflicting_fixed'][0]['y']), (0, 1))

    def test_rule_saved_pins_reload_and_survive_undo(self):
        project, map_id = self.rule_project()
        pins = [{'x': 0, 'y': 0, 'tile': 1, 'flip_x': True}]
        preview = self.rule_preview(project, map_id, {'pins': pins})
        committed = self.mutate(project, f'maps/{map_id}/edge-rules/commit', {
            'region': preview['region'], 'seed': 7, 'mode': 'solve', 'pins': pins,
            'fingerprint': preview['fingerprint'],
        })
        stored = committed['tilemaps'][0]['rule_pins']
        self.assertEqual(stored, [{'x': 0, 'y': 0, 'tile': 1, 'flip_x': True, 'flip_y': False}])
        summary = self.call('GET', self.base(project) + f'/maps/{map_id}/edge-rules')
        self.assertEqual(summary['saved_pins'], stored)
        follow_up = self.rule_preview(committed, map_id, {'region': {'x': 0, 'y': 0, 'width': 2, 'height': 2}})
        self.assertEqual(follow_up['status'], 'solved')
        self.assertEqual(follow_up['stats']['pinned'], 1)
        undone = self.mutate(committed, 'undo')
        self.assertNotIn('rule_pins', undone['tilemaps'][0])
        self.assertEqual(undone['tilemaps'][0]['edge_rules'], project['tilemaps'][0]['edge_rules'])

    def test_rule_flips_swap_edges_when_solving(self):
        project, map_id = self.rule_project()
        plain = self.rule_preview(project, map_id, {'region': {'x': 0, 'y': 0, 'width': 2, 'height': 2}})
        flipped = self.rule_preview(project, map_id, {
            'region': {'x': 0, 'y': 0, 'width': 2, 'height': 2},
            'pins': [{'x': 0, 'y': 0, 'tile': 1, 'flip_x': True}],
        })
        east_flip = next(item for item in flipped['connections'] if item['first'] == [0, 0] and item['direction'] == 'east')
        plain_east = next(item for item in plain['connections'] if item['first'] == [0, 0] and item['direction'] == 'east')
        self.assertEqual(flipped['stats']['pinned'], 1)
        self.assertEqual(east_flip['tokens'][0], 'W')
        self.assertEqual(plain_east['tokens'], [plain_east['tokens'][0], plain_east['tokens'][0]])

    def test_rule_conflict_isolates_one_bad_pin(self):
        project, map_id = self.rule_project()
        pins = [{'x': 0, 'y': 0, 'tile': 1}, {'x': 0, 'y': 1, 'tile': 3}, {'x': 2, 'y': 2, 'tile': 3}]
        solved = self.rule_preview(project, map_id, {'region': {'x': 0, 'y': 0, 'width': 3, 'height': 3}, 'pins': pins})
        self.assertEqual(solved['status'], 'conflict')
        core = [(pin['x'], pin['y']) for pin in solved['conflicting_pins']]
        self.assertLessEqual(len(core), 2)
        self.assertNotIn((2, 2), core)

    def test_rule_propagate_reports_undetermined_cells(self):
        project, map_id = self.rule_project()
        result = self.rule_preview(project, map_id, {'mode': 'propagate', 'region': {'x': 0, 'y': 0, 'width': 4, 'height': 4}})
        self.assertEqual(result['status'], 'solved')
        self.assertEqual(result['stats']['assigned'], 0)
        self.assertEqual(result['stats']['undetermined'], 16)
        self.assertEqual(len(result['undetermined']), 16)
        self.assertGreater(result['undetermined'][0]['candidate_count'], 1)
        self.assertEqual(result['connections'], [])
        pinned = self.rule_preview(project, map_id, {'mode': 'propagate', 'pins': [{'x': 1, 'y': 1, 'tile': 2}], 'region': {'x': 0, 'y': 0, 'width': 4, 'height': 4}})
        self.assertEqual(pinned['stats']['pinned'], 1)
        self.assertLess(pinned['stats']['undetermined'], 16)
        response = self.client.post(self.base(project) + f'/maps/{map_id}/edge-rules/commit', json={
            'revision': project['revision'], 'region': {'x': 0, 'y': 0, 'width': 4, 'height': 4}, 'mode': 'propagate',
        })
        self.assertEqual(response.status_code, 400)

    def test_rule_commit_rejects_stale_fingerprint(self):
        project, map_id = self.rule_project()
        preview = self.rule_preview(project, map_id, {})
        stale = self.mutate(project, f'maps/{map_id}/paint', {'tile': 3, 'cells': [[4, 3]]})
        response = self.client.post(self.base(stale) + f'/maps/{map_id}/edge-rules/commit', json={
            'revision': stale['revision'], 'region': preview['region'], 'seed': 7, 'mode': 'solve',
            'fingerprint': preview['fingerprint'],
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.get(project['id'])['revision'], stale['revision'])

    def test_rule_border_and_outside_cells_stay_unchanged(self):
        project, map_id = self.rule_project()
        project = self.mutate(project, f'maps/{map_id}/paint', {'tile': 2, 'cells': [[0, 1]]})
        before = project['tilemaps'][0]['layers'][0]['data'][:]
        preview = self.rule_preview(project, map_id, {'region': {'x': 1, 'y': 0, 'width': 2, 'height': 3}})
        self.assertEqual(preview['status'], 'solved')
        self.assertTrue(preview['fixed_border'])
        self.assertEqual((preview['fixed_border'][0]['x'], preview['fixed_border'][0]['y']), (0, 1))
        committed = self.mutate(project, f'maps/{map_id}/edge-rules/commit', {
            'layer_id': preview['layer_id'], 'region': preview['region'], 'seed': 7, 'mode': 'solve',
            'pins': [], 'fingerprint': preview['fingerprint'],
        })
        data = committed['tilemaps'][0]['layers'][0]['data']
        for index, value in enumerate(data):
            x, y = index % 5, index // 5
            if not (1 <= x <= 2):
                self.assertEqual(value, before[index], (x, y))

    def test_rule_rules_validation_and_clear(self):
        project, map_id = self.rule_project()
        summary = self.call('GET', self.base(project) + f'/maps/{map_id}/edge-rules')
        self.assertTrue(summary['configured'])
        self.assertEqual(summary['rules']['name'], '地形接口')
        for bad in [
            {'rules': {'name': '空', 'tiles': {}}},
            {'rules': {'name': '未知边', 'tiles': {'1': {'up': 'G'}}}},
            {'rules': {'name': '越界', 'tiles': {'99': {'north': 'G'}}}},
            {'rules': {'name': '全通配', 'tiles': {'1': {'north': '*', 'east': '*', 'south': '*', 'west': '*'}}}},
            {'rules': {'name': '布尔', 'tiles': {'true': {'north': 'G'}}}},
        ]:
            response = self.client.post(self.base(project) + f'/maps/{map_id}/edge-rules', json={'revision': project['revision'], **bad})
            self.assertEqual(response.status_code, 400, bad)
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])
        cleared = self.mutate(project, f'maps/{map_id}/edge-rules', {'rules': None})
        self.assertNotIn('edge_rules', cleared['tilemaps'][0])
        response = self.client.post(self.base(cleared) + f'/maps/{map_id}/edge-rules/preview', json={'revision': cleared['revision']})
        self.assertEqual(response.status_code, 400)

    def test_rule_pins_flip_and_invalid_input(self):
        project, map_id = self.rule_project()
        flipped = self.rule_preview(project, map_id, {'pins': [[0, 0, 1, True, False]], 'region': {'x': 0, 'y': 0, 'width': 2, 'height': 2}})
        self.assertEqual(flipped['status'], 'solved')
        cell = next(item for item in flipped['cells'] if (item['x'], item['y']) == (0, 0))
        self.assertEqual(cell['status'], 'pinned')
        self.assertEqual(cell['value'], 0x80000001)
        for bad in [
            {'pins': [[0, 0, 1], [0, 0, 2]]},
            {'pins': [[9, 9, 1]]},
            {'pins': [[0.5, 0, 1]]},
            {'region': {'x': 0, 'y': 0, 'width': 70, 'height': 70}},
            {'seed': -1},
            {'mode': 'unknown'},
        ]:
            payload = {'revision': project['revision'], 'region': {'x': 0, 'y': 0, 'width': 2, 'height': 2}}
            payload.update(bad)
            response = self.client.post(self.base(project) + f'/maps/{map_id}/edge-rules/preview', json=payload)
            self.assertEqual(response.status_code, 400, bad)

    def test_tilemap_reference_blocks_frame_deletion(self):
        project = self.demo('tiles')
        project = self.mutate(project, 'maps', {})
        response = self.client.post(self.base(project) + '/frames/remove', json={'revision': project['revision'], 'frame_ids': [project['frames'][0]['id']]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.store.get(project['id'])['frames']), 3)

    def test_named_versions_restore_fork_compare(self):
        project = self.paint(self.new())
        first = self.call('POST', self.base(project) + '/snapshots', {'name': '红色', 'revision': project['revision']}, 201)
        project = self.paint(project, '#0000ffff')
        second = self.call('POST', self.base(project) + '/snapshots', {'name': '蓝色'}, 201)
        diff = self.call('POST', self.base(project) + '/snapshots/compare', {'first': first['id'], 'second': second['id']})
        self.assertTrue(diff['changed_frames'][0]['pixel_assets_changed'])
        project = self.mutate(project, f'snapshots/{first["id"]}/restore')
        self.assertEqual(self.image(project).getpixel((6, 6)), (255, 0, 0, 255))
        fork = self.call('POST', self.base(project) + f'/snapshots/{second["id"]}/fork', {}, 201)
        self.assertEqual(self.image(fork).getpixel((6, 6)), (0, 0, 255, 255))

    def test_time_resample_total_preserved(self):
        project = self.demo()
        result = self.call('POST', self.base(project) + '/resample', {'fps': 20, 'mode': 'crossfade'}, 201)
        target = result['project']
        self.assertEqual(sum(frame['duration'] for frame in target['frames']), 1080)
        self.assertEqual(len(target['frames']), 22)
        self.assertEqual(len(self.store.get(project['id'])['frames']), 12)

    def test_ninepatch_preserves_corners(self):
        image = Image.new('RGBA', (16, 16), 'white')
        image.paste('red', (0, 0, 4, 4))
        image.paste('blue', (12, 12, 16, 16))
        output = nine_slice(image, (32, 24), (4, 4, 4, 4))
        self.assertEqual(output.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(output.getpixel((31, 23)), (0, 0, 255, 255))
        self.assertEqual(output.size, (32, 24))

    def test_seam_blend_matches_opposite_edges(self):
        array = np.zeros((16, 16, 4), dtype=np.uint8)
        array[:, :, 3] = 255
        array[:, 0, 0] = 255
        array[:, -1, 2] = 255
        output = np.asarray(seam_blend(Image.fromarray(array), 3))
        np.testing.assert_array_equal(output[:, 0], output[:, -1])
        np.testing.assert_array_equal(output[0], output[-1])

    def test_matte_and_auto_collision(self):
        project = self.paint(self.new(), '#00ff00ff')
        frame = project['frames'][0]
        project = self.mutate(project, 'matte', {'operation': 'chroma', 'frame_id': frame['id'], 'layer_id': frame['layers'][0]['id'], 'color': '#00ff00ff', 'tolerance': 0, 'softness': 0})
        self.assertEqual(self.image(project).getpixel((6, 6))[3], 0)
        project = self.paint(project)
        project = self.mutate(project, 'collision-boxes', {'frame_ids': [project['frames'][0]['id']], 'mode': 'union'})
        self.assertEqual(project['frames'][0]['boxes'][0], {'name': '碰撞 1', 'x': 4, 'y': 4, 'width': 9, 'height': 9})

    def test_auto_sprite_slice(self):
        image = Image.new('RGBA', (64, 32))
        painter = ImageDraw.Draw(image)
        painter.rectangle((2, 2, 12, 15), fill='red')
        painter.rectangle((40, 5, 54, 20), fill='blue')
        response = self.client.post('/api/auto-slice', data={'file': (io.BytesIO(png_bytes(image)), 'sprites.png'), 'parameters': json.dumps({'mode': 'gutters'})})
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(len(response.json['project']['frames']), 2)
        self.assertEqual(len(response.json['regions']), 2)

    def test_export_jobs_all_formats(self):
        project = self.demo()
        for kind in ['apng', 'gif', 'webp', 'atlas', 'multipage', 'sheet', 'sequence', 'project', 'palette']:
            job = self.call('POST', self.base(project) + '/jobs', {'format': kind, 'revision': project['revision']}, 202)
            self.app.extensions['jobs'].futures[job['id']].result(timeout=30)
            finished = self.call('GET', '/api/jobs/' + job['id'])
            self.assertEqual(finished['status'], 'completed', finished)
            data = self.call('GET', '/api/jobs/' + job['id'] + '/download')
            self.assertEqual(len(data), finished['bytes'])

    def test_revision_conflict_and_invalid_inputs(self):
        project = self.paint(self.new())
        self.call('PATCH', self.base(project), {'revision': 0, 'name': 'stale'}, 409)
        for payload in [{'tool': 'pencil', 'points': [[1000, 0]]}, {'tool': 'pencil', 'points': [[1, 1]], 'size': True}, {'tool': 'unknown', 'points': [[1, 1]]}]:
            response = self.client.post(self.base(project) + '/' + self.layer_path(project, 'draw'), json={'revision': project['revision'], **payload})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])

    def test_pipeline_atomic_rollback(self):
        project = self.paint(self.new())
        original = np.asarray(self.image(project)).copy()
        response = self.client.post(self.base(project) + '/pipeline', json={'revision': project['revision'], 'frame_ids': [project['frames'][0]['id']], 'steps': [{'operation': 'invert'}, {'operation': 'not-real'}]})
        self.assertEqual(response.status_code, 400)
        actual = self.store.get(project['id'])
        self.assertEqual(actual['revision'], project['revision'])
        np.testing.assert_array_equal(original, np.asarray(self.image(actual)))

    def test_app_reload_persistence(self):
        project = self.paint(self.new())
        another = create_app(self.temp.name)
        try:
            client = another.test_client()
            response = client.get(self.base(project))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['revision'], project['revision'])
            actual = another.extensions['store'].get(project['id'])
            self.assertEqual(actual['frames'][0]['layers'][0]['asset'], project['frames'][0]['layers'][0]['asset'])
        finally:
            another.extensions['jobs'].executor.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
