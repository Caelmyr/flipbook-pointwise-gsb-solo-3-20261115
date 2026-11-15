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

    def mask_pair(self, project, color='#ff0000ff', box=(4, 10, 9, 19)):
        frame = project['frames'][0]
        return self.mutate(project, self.layer_path(project, 'draw'), {
            'tool': 'rectangle', 'color': color, 'points': [list(box[:2]), list(box[2:])],
            'filled': True,
        })

    def morph_setup(self):
        project = self.paint(self.new(), '#ff0000ff')
        project = self.mutate(project, 'frames', {'after': project['frames'][0]['id'], 'count': 1})
        second = project['frames'][1]
        project = self.mutate(project, 'frames/' + second['id'] + '/layers/' + second['layers'][0]['id'] + '/draw',
                              {'tool': 'rectangle', 'color': '#0000ffff', 'points': [[20, 10], [25, 19]], 'filled': True})
        return project

    def morph_body(self, project, pairs, **extra):
        payload = {'first': project['frames'][0]['id'], 'second': project['frames'][1]['id'],
                   'pairs': pairs, 'count': 4, 'duration': 80, 'easing': 'linear', 'merge': 'dither'}
        payload.update(extra)
        payload['revision'] = project['revision']
        return payload

    def test_morph_generates_intermediate_frames_and_keeps_originals(self):
        project = self.morph_setup()
        originals = [self.image(project, 0).tobytes(), self.image(project, 1).tobytes()]
        body = self.morph_body(project, [
            {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
            {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
            {'label': '左下', 'first': {'x': 4, 'y': 19}, 'second': {'x': 20, 'y': 19}},
            {'label': '右上', 'first': {'x': 9, 'y': 10}, 'second': {'x': 25, 'y': 10}},
        ])
        result = self.call('POST', self.base(project) + '/morph', body)
        generated = result['project']
        self.assertEqual(len(generated['frames']), 6)
        self.assertEqual([frame['name'] for frame in generated['frames'][:2]], ['帧 1', '过渡 1 / 4'])
        np.testing.assert_array_equal(np.asarray(self.image(generated, 0)), np.asarray(Image.frombytes('RGBA', (32, 32), originals[0])))
        np.testing.assert_array_equal(np.asarray(self.image(generated, -1)), np.asarray(Image.frombytes('RGBA', (32, 32), originals[1])))
        self.assertEqual([frame['duration'] for frame in generated['frames']],
                         [generated['frames'][0]['duration'], 80, 80, 80, 80, generated['frames'][-1]['duration']])
        # The in-between actually moves: the left edge travels from x=4 to x=20.
        positions = []
        for index in range(1, 5):
            alpha = np.asarray(self.image(generated, index))[:, :, 3]
            columns = np.nonzero(alpha.any(axis=0))[0]
            positions.append(int(columns.min()))
        self.assertGreaterEqual(positions[0], 4)
        self.assertLess(positions[0], positions[-1])
        self.assertGreater(positions[-1], positions[0])
        self.assertTrue(result['order']['contiguous'])
        self.assertEqual(len(result['order']['rows']), 6)
        self.assertEqual([row['role'] for row in result['order']['rows']],
                         ['first', 'generated', 'generated', 'generated', 'generated', 'second'])
        self.assertEqual(sum(row['duration'] for row in result['order']['rows'] if row['role'] == 'generated'), 4 * 80)

    def test_morph_alpha_follows_shape_not_opacity(self):
        project = self.morph_setup()
        body = self.morph_body(project, [
            {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
            {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
            {'label': '左下', 'first': {'x': 4, 'y': 19}, 'second': {'x': 20, 'y': 19}},
            {'label': '右上', 'first': {'x': 9, 'y': 10}, 'second': {'x': 25, 'y': 10}},
        ], merge='blend')
        result = self.call('POST', self.base(project) + '/morph', body)['project']
        for index in range(1, 5):
            alpha = np.asarray(self.image(result, index))[:, :, 3]
            partial = int(((alpha > 0) & (alpha < 255)).sum())
            self.assertEqual(partial, 0, '过渡帧不应靠半透明像素掩盖错位')
        dither = self.call('POST', self.base(project) + '/morph', self.morph_body(project, [
            {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
            {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
            {'label': '左下', 'first': {'x': 4, 'y': 19}, 'second': {'x': 20, 'y': 19}},
            {'label': '右上', 'first': {'x': 9, 'y': 10}, 'second': {'x': 25, 'y': 10}},
        ]))['project'] if False else None

    def test_morph_reports_conflicting_correspondence(self):
        project = self.morph_setup()
        for pairs, kind in [
            ([{'label': '甲', 'first': {'x': 4, 'y': 4}, 'second': {'x': 28, 'y': 4}},
              {'label': '乙', 'first': {'x': 28, 'y': 4}, 'second': {'x': 4, 'y': 4}},
              {'label': '丙', 'first': {'x': 4, 'y': 28}, 'second': {'x': 4, 'y': 28}},
              {'label': '丁', 'first': {'x': 28, 'y': 28}, 'second': {'x': 28, 'y': 28}}], 'fold'),
        ]:
            response = self.client.post(self.base(project) + '/morph/plan', json=self.morph_body(project, pairs))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['conflict'])
            self.assertTrue(any(item['kind'] == kind for item in response.json['problems']))
            self.assertTrue(all(item['pairs'] for item in response.json['problems']))
            response.close()
            blocked = self.client.post(self.base(project) + '/morph', json=self.morph_body(project, pairs))
            self.assertEqual(blocked.status_code, 400)
            self.assertIn('对应关系冲突', blocked.json['error'])
            blocked.close()
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])

    def test_morph_recompute_replaces_same_pair_only(self):
        project = self.morph_setup()
        pairs = [
            {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
            {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
            {'label': '左下', 'first': {'x': 4, 'y': 19}, 'second': {'x': 20, 'y': 19}},
            {'label': '右上', 'first': {'x': 9, 'y': 10}, 'second': {'x': 25, 'y': 10}},
        ]
        first = self.call('POST', self.base(project) + '/morph', self.morph_body(project, pairs))
        project = first['project']
        generated_ids = first['order']['frames']
        other_frames = [frame['id'] for frame in project['frames'] if frame['id'] not in generated_ids]
        adjusted = [dict(pair) for pair in pairs]
        adjusted[1] = {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 21, 'y': 19}}
        adjusted.append({'label': '中点', 'first': {'x': 6, 'y': 14}, 'second': {'x': 22, 'y': 14}})
        body = self.morph_body(project, adjusted, group=first['group'])
        result = self.call('POST', self.base(project) + '/morph/recompute', body)
        updated = result['project']
        self.assertEqual(len(updated['frames']), len(project['frames']))
        self.assertEqual([frame['id'] for frame in updated['frames']], [frame['id'] for frame in project['frames']])
        self.assertTrue(set(other_frames) <= {frame['id'] for frame in updated['frames']})
        self.assertEqual(result['order']['frames'], generated_ids)
        self.assertEqual(len(result['order']['frames']), 4)
        # Same frames, recomputed pixels: the added middle feature changes the
        # geometry, so the recomputed in-betweens differ from the first run.
        self.assertFalse(np.array_equal(np.asarray(self.image(project, 3)), np.asarray(self.image(updated, 3))))
        self.assertEqual(self.call('GET', self.base(updated) + '/morph')[0]['count'], 4)

    def test_morph_recompute_rejects_edited_originals(self):
        project = self.morph_setup()
        pairs = [
            {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
            {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
            {'label': '左下', 'first': {'x': 4, 'y': 19}, 'second': {'x': 20, 'y': 19}},
            {'label': '右上', 'first': {'x': 9, 'y': 10}, 'second': {'x': 25, 'y': 10}},
        ]
        generated = self.call('POST', self.base(project) + '/morph', self.morph_body(project, pairs))
        project = generated['project']
        group = generated['group']
        altered = self.mutate(project, 'frames/' + project['frames'][0]['id'] + '/layers/' + project['frames'][0]['layers'][0]['id'] + '/draw',
                              {'tool': 'pencil', 'color': '#00ff00ff', 'points': [[1, 1]]})
        response = self.client.post(self.base(altered) + '/morph/recompute',
                                    json={**self.morph_body(altered, pairs), 'group': group})
        self.assertEqual(response.status_code, 400)
        self.assertIn('原画已被修改', response.json['error'])
        response.close()
        self.assertEqual(len(self.store.get(altered['id'])['frames']), len(altered['frames']))

    def test_morph_plan_lists_duration_and_order_before_generating(self):
        project = self.morph_setup()
        body = self.morph_body(project, [
            {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
            {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
        ], total=400, count=4)
        plan = self.call('POST', self.base(project) + '/morph/plan', body)
        self.assertIsNone(plan['conflict'])
        self.assertEqual(plan['count'], 4)
        self.assertEqual(plan['total'], 400)
        self.assertEqual([row['position'] for row in plan['rows']], ['original', '1/4', '2/4', '3/4', '4/4', 'original'])
        self.assertEqual([row['role'] for row in plan['rows']], ['first', 'generated', 'generated', 'generated', 'generated', 'second'])
        self.assertEqual(plan['insertion_index'], 1)
        self.assertTrue(plan['adjacent_originals'])
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])
        preview = self.client.post(self.base(project) + '/morph/preview', json=body)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers['Content-Type'], 'image/png')
        preview.close()
        generated = self.call('POST', self.base(project) + '/morph', body)['project']
        self.assertEqual([frame['duration'] for frame in generated['frames'][1:5]], [100, 100, 100, 100])

    def test_morph_reveal_window_keeps_one_local_copy(self):
        project = self.new(48, 48)
        project = self.mutate(project, self.layer_path(project, 'draw'), {'tool': 'pencil', 'color': '#ff0000ff', 'points': [[6, 6], [6, 10]]})
        project = self.mutate(project, 'frames', {'after': project['frames'][0]['id'], 'count': 1})
        second = project['frames'][1]
        project = self.mutate(project, 'frames/' + second['id'] + '/layers/' + second['layers'][0]['id'] + '/draw',
                              {'tool': 'pencil', 'color': '#0000ffff', 'points': [[12, 6], [12, 10]]})
        pairs = [
            {'label': '眼', 'first': {'x': 6, 'y': 6}, 'second': {'x': 12, 'y': 6}},
            {'label': '尾', 'first': {'x': 6, 'y': 10}, 'second': {'x': 12, 'y': 10}},
        ]
        plain = self.call('POST', self.base(project) + '/morph', self.morph_body(project, pairs, count=4, duration=60))['project']
        reveal = [dict(pair) for pair in pairs]
        reveal[0] = {**pairs[0], 'reveal': {'from': 'first', 'at': 0.9, 'feather': 0,
                                           'x': 2, 'y': 2, 'width': 24, 'height': 24}}
        held = self.call('POST', self.base(project) + '/morph', self.morph_body(plain, reveal, count=4, duration=60))['project']
        # Without the window the whole drawing has already switched by 3/5.
        midpoint = 1 + 2
        outside_plain = np.asarray(self.image(plain, midpoint))[:, :, 3]
        outside_held = np.asarray(self.image(held, midpoint))[:, :, 3]
        self.assertGreater(int(np.count_nonzero(outside_plain)), 0)
        # A window that keeps the first drawing locally makes the same moment
        # contain the local first-drawing content the plain run has already lost.
        red_plain = int(np.count_nonzero((np.asarray(self.image(plain, midpoint))[:, :, 0] == 255)
                                         & (np.asarray(self.image(plain, midpoint))[:, :, 3] > 0)))
        red_held = int(np.count_nonzero((np.asarray(self.image(held, midpoint))[:, :, 0] == 255)
                                        & (np.asarray(self.image(held, midpoint))[:, :, 3] > 0)))
        self.assertGreater(red_held, red_plain)
        # And the silhouette still travels with the stroke, not faded out.
        alpha = outside_held[outside_held > 0]
        self.assertTrue(np.all(alpha == 255))

    def test_morph_moves_features_instead_of_doubling_them(self):
        """The original complaint: a plain crossfade shows two eyes and two outlines."""
        project = self.new(48, 48)
        frame = project['frames'][0]
        project = self.mutate(project, self.layer_path(project, 'draw'),
                              {'tool': 'rectangle', 'color': '#ff0000ff', 'points': [[6, 20], [10, 28]], 'filled': True})
        project = self.mutate(project, 'frames', {'after': project['frames'][0]['id'], 'count': 1})
        second = project['frames'][1]
        project = self.mutate(project, 'frames/' + second['id'] + '/layers/' + second['layers'][0]['id'] + '/draw',
                              {'tool': 'rectangle', 'color': '#ff0000ff', 'points': [[36, 20], [40, 28]], 'filled': True})
        body = self.morph_body(project, [
            {'label': '眼左上', 'first': {'x': 6, 'y': 20}, 'second': {'x': 36, 'y': 20}},
            {'label': '眼右下', 'first': {'x': 10, 'y': 28}, 'second': {'x': 40, 'y': 28}},
            {'label': '眼左下', 'first': {'x': 6, 'y': 28}, 'second': {'x': 36, 'y': 28}},
            {'label': '眼右上', 'first': {'x': 10, 'y': 20}, 'second': {'x': 40, 'y': 20}},
        ], count=4, duration=60)
        generated = self.call('POST', self.base(project) + '/morph', body)['project']

        def clusters(image):
            mask = np.asarray(image)[:, :, 3] > 0
            seen = set()
            count = 0
            for y, x in zip(*np.nonzero(mask)):
                if (y, x) in seen:
                    continue
                count += 1
                stack = [(y, x)]
                seen.add((y, x))
                while stack:
                    cy, cx = stack.pop()
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny, nx] and (ny, nx) not in seen:
                            seen.add((ny, nx))
                            stack.append((ny, nx))
            return count

        # Each in-between shows one moving shape, not two separate copies.
        for index in range(1, 5):
            self.assertEqual(clusters(self.image(generated, index)), 1)
        # A plain crossfade at the same moment would show two.
        from flipbook.tween import premultiplied_crossfade
        blended = premultiplied_crossfade(self.image(generated, 0), self.image(generated, -1), .5)
        self.assertEqual(clusters(blended), 2)
        # And the moving shape is opaque throughout, so it is not hidden by alpha.
        for index in range(1, 5):
            alpha = np.asarray(self.image(generated, index))[:, :, 3]
            self.assertTrue(np.all(alpha[alpha > 0] == 255))

    def test_morph_group_dropped_when_generated_frame_removed(self):
        project = self.morph_setup()
        body = self.morph_body(project, [
            {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
            {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
            {'label': '左下', 'first': {'x': 4, 'y': 19}, 'second': {'x': 20, 'y': 19}},
            {'label': '右上', 'first': {'x': 9, 'y': 10}, 'second': {'x': 25, 'y': 10}},
        ])
        generated = self.call('POST', self.base(project) + '/morph', body)
        project = generated['project']
        group = generated['group']
        kept = project['frames'][0]['id']
        project = self.mutate(project, 'frames/remove', {'frame_ids': [project['frames'][2]['id']]})
        self.assertEqual(project['morph_groups'], [])
        self.assertEqual(self.call('GET', self.base(project) + '/morph'), [])
        # The removed frame is gone and the originals are untouched.
        self.assertEqual(len(project['frames']), 5)
        self.assertEqual(project['frames'][0]['id'], kept)
        response = self.client.post(self.base(project) + '/morph/recompute',
                                    json={**self.morph_body(project, [
                                        {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
                                        {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
                                    ]), 'group': group})
        self.assertEqual(response.status_code, 400)
        self.assertIn('来源记录', response.json['error'])
        response.close()

    def test_morph_group_survives_project_archive_roundtrip(self):
        project = self.morph_setup()
        body = self.morph_body(project, [
            {'label': '左上', 'first': {'x': 4, 'y': 10}, 'second': {'x': 20, 'y': 10}},
            {'label': '右下', 'first': {'x': 9, 'y': 19}, 'second': {'x': 25, 'y': 19}},
            {'label': '左下', 'first': {'x': 4, 'y': 19}, 'second': {'x': 20, 'y': 19}},
            {'label': '右上', 'first': {'x': 9, 'y': 10}, 'second': {'x': 25, 'y': 10}},
        ])
        generated = self.call('POST', self.base(project) + '/morph', body)
        project = generated['project']
        bundle = project_bundle(self.store, project)
        response = self.client.post('/api/import-bundle', data={'file': (io.BytesIO(bundle), 'project.zip')})
        self.assertEqual(response.status_code, 201, response.json)
        response.close()
        restored = self.store.get(response.json['id'])
        self.assertEqual(len(restored['morph_groups']), 1)
        self.assertEqual(restored['morph_groups'][0]['count'], 4)
        self.assertEqual(restored['morph_groups'][0]['pairs'], project['morph_groups'][0]['pairs'])

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
