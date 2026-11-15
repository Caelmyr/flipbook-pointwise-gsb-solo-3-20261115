import io
import json
import sys
import tempfile
import unittest
import zipfile
from copy import deepcopy
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
from flipbook.panels import solve_layout, default_grid, validate_margins
from flipbook.panel_build import rasterize_layer
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

    def _panel_source(self):
        project = self.new(32, 32)
        frame_id = project['frames'][0]['id']
        base_id = project['frames'][0]['layers'][0]['id']

        def draw(layer_id, color, points):
            current = self.store.get(project['id'])
            return self.mutate(current, f'frames/{frame_id}/layers/{layer_id}/draw', {
                'tool': 'rectangle', 'color': color, 'points': points, 'filled': True})

        project = draw(base_id, '#ffffffff', [[0, 0], [31, 31]])
        project = draw(base_id, '#ff0000ff', [[0, 0], [5, 5]])
        project = draw(base_id, '#0000ffff', [[26, 26], [31, 31]])
        project = self.mutate(project, f'frames/{frame_id}/layers', {'name': '按钮层'})
        button_id = project['frames'][0]['layers'][-1]['id']
        project = draw(button_id, '#00ff00ff', [[4, 13], [27, 18]])
        return project

    def _create_panel(self, source, decorations=None, width=64, height=48):
        payload = {'name': '面板作品', 'width': width, 'height': height,
                   'left': 6, 'right': 6, 'top': 6, 'bottom': 6}
        if decorations is not None:
            payload['decorations'] = decorations
        return self.call('POST', self.base(source) + '/panels', payload, 201)

    def test_panel_keeps_layers_and_fixed_corners(self):
        source = self._panel_source()
        panel = self._create_panel(source, [
            {'id': 'd1', 'name': '角饰', 'rect': [0, 0, 4, 4],
             'attachment': {'target': 'edge', 'anchor': 'top_left'}},
            {'id': 'd2', 'name': '跟随钉', 'rect': [0, 0, 2, 2],
             'attachment': {'target': 'd1', 'anchor': 'bottom_right'}},
        ])
        self.assertEqual((panel['width'], panel['height']), (64, 48))
        self.assertEqual(len(panel['frames'][0]['layers']), 2)
        self.assertTrue(all(l.get('panel_source') for l in panel['frames'][0]['layers']))
        image = self.image(panel)
        self.assertEqual(image.getpixel((2, 2)), (255, 0, 0, 255))
        self.assertEqual(image.getpixel((63, 47)), (0, 0, 255, 255))
        self.assertNotEqual(image.getpixel((7, 2)), (255, 0, 0, 255))
        placed = panel['frames'][0]['panel_workspace']['last_feasible']['decorations']
        self.assertEqual(placed['d1'], [0, 0, 4, 4])
        self.assertEqual(placed['d2'], [4, 4, 2, 2])

    def test_panel_repeated_resize_uses_pristine_originals(self):
        source = self._panel_source()
        panel = self._create_panel(source)
        panel_id = panel['id']
        for width, height in [(80, 60), (48, 40), (96, 72), (64, 48)]:
            panel = self.mutate(panel, 'panel/resize', {'width': width, 'height': height})
            image = self.image(panel)
            self.assertEqual(image.size, (width, height))
            self.assertEqual(image.getpixel((2, 2)), (255, 0, 0, 255))
            self.assertEqual(image.getpixel((width - 1, height - 1)), (0, 0, 255, 255))
        # pristine originals are retained at the source size, not the canvas size
        meta = panel['frames'][0]['layers'][0]['panel_source']
        self.assertEqual(self.store.image(panel_id, meta['asset']).size, (32, 32))
        self.assertTrue(self.call('GET', self.base(panel) + '/integrity')['valid'])

    def test_panel_conflict_keeps_last_feasible_result(self):
        source = self._panel_source()
        panel = self._create_panel(source)
        revision_before = panel['revision']
        size_before = (panel['width'], panel['height'])
        panel = self.call('PATCH', self.base(panel) + '/panel', {
            'revision': revision_before,
            'decorations': [{
                'id': 'd3', 'name': '居中徽章', 'rect': [0, 0, 20, 6],
                'attachment': {'target': 'edge', 'anchor': 'top'},
                'center_x': True, 'min_gap': [10, 0, 10, 0],
            }],
        })
        response = self.client.post(self.base(panel) + '/panel/resize', json={
            'revision': panel['revision'], 'width': 24, 'height': 48})
        self.assertEqual(response.status_code, 400)
        self.assertIn('冲突', response.json['error'])
        self.assertIn('居中', response.json['error'])
        stored = self.store.get(panel['id'])
        self.assertEqual(stored['revision'], panel['revision'])
        self.assertEqual((stored['width'], stored['height']), size_before)
        check = self.call('GET', self.base(panel) + '/panel/preview?check=true&width=24&height=48')
        self.assertFalse(check['feasible'])
        self.assertTrue(self.call('GET', self.base(panel) + '/panel/preview?check=true&width=80&height=48')['feasible'])

    def test_panel_hand_edit_warns_before_recompute(self):
        source = self._panel_source()
        panel = self._create_panel(source)
        layer = panel['frames'][0]['layers'][0]
        panel = self.mutate(panel, f'frames/{panel["frames"][0]["id"]}/layers/{layer["id"]}/draw', {
            'tool': 'pencil', 'color': '#ffff00ff', 'points': [[10, 10]]})
        self.assertTrue(panel['frames'][0]['layers'][0].get('panel_diverged'))
        response = self.client.post(self.base(panel) + '/panel/resize', json={
            'revision': panel['revision'], 'width': 70, 'height': 50})
        self.assertEqual(response.status_code, 400)
        self.assertIn('手动修改', response.json['error'])
        panel = self.mutate(panel, 'panel/resize',
                           {'width': 70, 'height': 50, 'confirm_overwrite': True})
        self.assertFalse(panel['frames'][0]['layers'][0].get('panel_diverged'))

    def test_panel_preview_reopen_export_share_layout(self):
        source = self._panel_source()
        panel = self._create_panel(source)
        panel = self.mutate(panel, 'panel/resize', {'width': 72, 'height': 56})
        before = np.asarray(self.image(panel)).copy()
        reloaded_app = create_app(self.temp.name)
        try:
            reloaded = reloaded_app.extensions['store'].get(panel['id'])
            np.testing.assert_array_equal(
                np.asarray(render_frame(reloaded_app.extensions['store'], reloaded, reloaded['frames'][0]['id'])), before)
        finally:
            reloaded_app.extensions['jobs'].executor.shutdown(wait=True)
        bundle = project_bundle(self.store, panel)
        response = self.client.post('/api/import-bundle',
                                    data={'file': (io.BytesIO(bundle), 'panel.zip')})
        self.assertEqual(response.status_code, 201)
        restored = response.json
        self.assertTrue(restored['frames'][0]['layers'][0]['panel_source']['asset'])
        np.testing.assert_array_equal(np.asarray(self.image(restored)), before)

    def test_panel_repeat_grid_tiles_without_stretching_fixed(self):
        source = self._panel_source()
        panel = self.call('POST', self.base(source) + '/panels', {
            'name': '平铺面板', 'width': 64, 'height': 32,
            'grid': {'columns': [
                {'id': 'a', 'behavior': 'fixed', 'span': 6},
                {'id': 'b', 'behavior': 'repeat', 'span': 8},
                {'id': 'c', 'behavior': 'stretch', 'span': 10},
                {'id': 'd', 'behavior': 'fixed', 'span': 8}],
                'rows': [
                {'id': 'r1', 'behavior': 'fixed', 'span': 6},
                {'id': 'r2', 'behavior': 'repeat', 'span': 20},
                {'id': 'r3', 'behavior': 'fixed', 'span': 6}]},
        }, 201)
        self.assertEqual((panel['width'], panel['height']), (64, 32))
        image = self.image(panel)
        self.assertEqual(image.getpixel((2, 2)), (255, 0, 0, 255))
        # a repeat-only axis must demand an exact-size target instead of a gap
        bad = self.client.post(self.base(source) + '/panels', json={
            'name': '无拉伸', 'width': 64, 'height': 40,
            'grid': {'columns': [
                {'id': 'a', 'behavior': 'fixed', 'span': 6},
                {'id': 'b', 'behavior': 'repeat', 'span': 20},
                {'id': 'c', 'behavior': 'fixed', 'span': 6}],
                'rows': [
                {'id': 'r1', 'behavior': 'fixed', 'span': 6},
                {'id': 'r2', 'behavior': 'repeat', 'span': 20},
                {'id': 'r3', 'behavior': 'fixed', 'span': 6}]}})
        self.assertEqual(bad.status_code, 400)
        self.assertIn('必须等于', bad.json['error'])

    def test_panel_layout_solver_rejects_cycle_and_bad_grid(self):
        workspace = {
            'source_size': [32, 32],
            'grid': default_grid(32, 32, validate_margins(32, 32, {'left': 6, 'right': 6, 'top': 6, 'bottom': 6})),
            'decorations': [
                {'id': 'a', 'name': '甲', 'rect': [0, 0, 4, 4], 'attachment': {'target': 'b', 'anchor': 'top_left'}},
                {'id': 'b', 'name': '乙', 'rect': [0, 0, 4, 4], 'attachment': {'target': 'a', 'anchor': 'top_left'}},
            ],
        }
        with self.assertRaises(ValueError):
            from flipbook.panels import validate_workspace
            validate_workspace(workspace)
        bad = deepcopy(workspace)
        bad['decorations'] = []
        bad['grid']['columns'][1]['span'] = 99
        with self.assertRaises(ValueError):
            from flipbook.panels import validate_workspace
            validate_workspace(bad)

    def test_rasterize_layer_maps_each_cell_independently(self):
        source_image = Image.new('RGBA', (16, 16), '#ffffffff')
        source_image.paste('#ff0000ff', (0, 0, 4, 4))
        workspace = {
            'source_size': [16, 16],
            'grid': default_grid(16, 16, (4, 4, 4, 4)),
            'decorations': [],
        }
        layout = solve_layout(workspace, 32, 24)
        output = rasterize_layer(source_image, layout['mapping'], 32, 24)
        self.assertEqual(output.size, (32, 24))
        self.assertEqual(output.getpixel((1, 1)), (255, 0, 0, 255))
        self.assertEqual(output.getpixel((3, 3)), (255, 0, 0, 255))
        self.assertNotEqual(output.getpixel((5, 1)), (255, 0, 0, 255))

    def test_panel_rejects_malformed_input_without_500(self):
        source = self._panel_source()
        before = len(self.call('GET', '/api/projects'))
        # malformed decoration (missing rect) must be a 400, not a crash
        response = self.client.post(self.base(source) + '/panels', json={
            'name': '坏面板', 'width': 64, 'height': 48,
            'left': 6, 'right': 6, 'top': 6, 'bottom': 6,
            'decorations': [{'id': 'x', 'name': '缺区域'}],
        })
        self.assertEqual(response.status_code, 400)
        # decoration rect outside the source canvas is rejected
        response = self.client.post(self.base(source) + '/panels', json={
            'name': '越界面板', 'width': 64, 'height': 48,
            'left': 6, 'right': 6, 'top': 6, 'bottom': 6,
            'decorations': [{'id': 'x', 'name': '越界', 'rect': [30, 30, 10, 10],
                             'attachment': {'target': 'edge', 'anchor': 'top_left'}}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('超出原稿画布', response.json['error'])
        # grid segments not summing to the source extent are rejected
        response = self.client.post(self.base(source) + '/panels', json={
            'name': '坏网格', 'width': 64, 'height': 48,
            'grid': {'columns': [{'behavior': 'fixed', 'span': 10}],
                     'rows': [{'behavior': 'stretch', 'span': 32}]}})
        self.assertEqual(response.status_code, 400)
        # failed creation must not leave a panel project behind
        self.assertEqual(len(self.call('GET', '/api/projects')), before)

    def test_panel_resize_rejects_layer_without_pristine_source(self):
        source = self._panel_source()
        panel = self._create_panel(source)
        # add an ordinary blank layer through the standard layer API
        panel = self.mutate(panel, f'frames/{panel["frames"][0]["id"]}/layers', {'name': '无原稿层'})
        self.assertFalse(panel['frames'][0]['layers'][-1].get('panel_source'))
        response = self.client.post(self.base(panel) + '/panel/resize', json={
            'revision': panel['revision'], 'width': 90, 'height': 60})
        self.assertEqual(response.status_code, 400)
        self.assertIn('没有分层原稿', response.json['error'])
        # the feasible result is untouched after the rejection
        stored = self.store.get(panel['id'])
        self.assertEqual((stored['width'], stored['height']), (64, 48))


if __name__ == '__main__':
    unittest.main(verbosity=2)
