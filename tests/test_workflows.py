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

    def part(self, box, color):
        image = Image.new('RGBA', (64, 64))
        ImageDraw.Draw(image).rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=color)
        return image

    def rigged(self):
        project = self.new(64, 64)
        frame = project['frames'][0]
        parts = [('躯干', (26, 20, 38, 44), '#66e2bd'), ('头', (28, 8, 36, 18), '#fa87ac'),
                 ('左腿', (27, 44, 31, 58), '#f8cc65'), ('右腿', (33, 44, 37, 58), '#8c9bff')]
        with self.store.lock:
            with self.store.edit(project['id'], project['revision'], '测试角色分区') as working:
                working_frame = working['frames'][0]
                working_frame['layers'] = []
                for name, box, color in parts:
                    from flipbook.model import new_layer
                    working_frame['layers'].append(new_layer(name, self.store.put_image(project['id'], self.part(box, color))))
            project = self.store.get(project['id'])
        frame = project['frames'][0]
        transforms = {item['name']: item['transform'] for item in frame['layers']}
        bones = [
            {'id': 'a' * 32, 'name': '根', 'parent': None, 'origin': [32, 54], 'angle': -90, 'length': 18, 'limit_min': -180, 'limit_max': 180, 'target': False},
            {'id': 'b' * 32, 'name': '躯干', 'parent': 'a' * 32, 'origin': [32, 38], 'angle': -90, 'length': 2, 'limit_min': -60, 'limit_max': 60, 'target': False},
            {'id': 'c' * 32, 'name': '左腿', 'parent': 'b' * 32, 'origin': [29, 36], 'angle': 90, 'length': 20, 'limit_min': -120, 'limit_max': 120, 'target': True},
            {'id': 'd' * 32, 'name': '右腿', 'parent': 'b' * 32, 'origin': [35, 36], 'angle': 90, 'length': 20, 'limit_min': -120, 'limit_max': 120, 'target': True},
        ]
        bindings = [{'bone': 'b' * 32, 'layer': '躯干', 'bind': transforms['躯干']},
                    {'bone': 'c' * 32, 'layer': '左腿', 'bind': transforms['左腿']},
                    {'bone': 'd' * 32, 'layer': '右腿', 'bind': transforms['右腿']}]
        rig = {'bones': bones, 'root': 'a' * 32, 'created_frame': frame['id'], 'bindings': bindings}
        project = self.mutate(project, 'rig', {'rig': rig}, 'PUT')
        return project, bones

    def test_rig_state_and_pose_writes_layer_transforms(self):
        project, bones = self.rigged()
        state = self.call('GET', self.base(project) + '/rig')
        self.assertEqual([item['name'] for item in state['bones']], ['根', '躯干', '左腿', '右腿'])
        self.assertEqual(state['bindings'][0]['bind'], project['rig']['bindings'][0]['bind'])
        frame = project['frames'][0]
        result = self.mutate(project, 'rig/pose', {'frame_id': frame['id'], 'pose': {
            'rotations': {'c' * 32: 30}, 'root': [3, 0], 'targets': []}})
        stored = result['project']['frames'][0]
        self.assertEqual(stored['pose']['rotations'], {'c' * 32: 30})
        self.assertEqual(stored['pose']['root'], [3.0, 0.0])
        leg = [item for item in stored['layers'] if item['name'] == '左腿'][0]
        self.assertNotEqual(leg['transform'], {'x': 0, 'y': 0, 'scale_x': 1, 'scale_y': 1, 'rotation': 0, 'flip_x': False, 'flip_y': False})
        torso = [item for item in stored['layers'] if item['name'] == '躯干'][0]
        self.assertEqual(torso['transform']['rotation'], 0)

    def test_rig_identity_pose_keeps_bind_transform(self):
        project, bones = self.rigged()
        frame = project['frames'][0]
        before = frame['layers']
        result = self.mutate(project, 'rig/pose', {'frame_id': frame['id'], 'pose': {'rotations': {}, 'root': [0, 0], 'targets': []}})
        after = result['project']['frames'][0]['layers']
        self.assertEqual([item['transform'] for item in before], [item['transform'] for item in after])
        moved = self.mutate(result['project'], 'rig/pose', {'frame_id': frame['id'], 'pose': {'rotations': {}, 'root': [4, -2], 'targets': []}})
        bound = {'躯干', '左腿', '右腿'}
        before_map = {item['name']: item['transform'] for item in before}
        after_map = {item['name']: item['transform'] for item in moved['project']['frames'][0]['layers']}
        for name in bound:
            self.assertEqual(after_map[name]['x'] - before_map[name]['x'], 4)
            self.assertEqual(after_map[name]['y'] - before_map[name]['y'], -2)
        self.assertEqual(after_map['头'], before_map['头'])

    def test_rig_joint_limits_clamp_rotation(self):
        project, bones = self.rigged()
        frame = project['frames'][0]
        report = self.mutate(project, 'rig/solve', {'pose': {'rotations': {'c' * 32: 200}, 'root': [0, 0], 'targets': []}})
        self.assertEqual(report['requested_rotations'], {'c' * 32: 200})
        self.assertIn('c' * 32, report['clamped'])
        leg = [item for item in report['bones'] if item['id'] == 'c' * 32][0]
        self.assertEqual(leg['delta'], 120)

    def test_rig_unreachable_target_reports_residual(self):
        project, bones = self.rigged()
        report = self.mutate(project, 'rig/solve', {'pose': {'rotations': {}, 'root': [0, 0], 'targets': [
            {'bone': 'c' * 32, 'target': [62, 4]}]}})
        target = report['targets'][0]
        self.assertGreater(target['residual'], 5)
        self.assertNotEqual(target['reached'], target['target'])
        leg = [item for item in report['bones'] if item['id'] == 'c' * 32][0]
        self.assertEqual(leg['length'], 20)
        layer = [item for item in report['layers'] if item['layer'] == '左腿'][0]
        self.assertEqual(layer['transform']['scale_x'], 1.0)
        self.assertEqual(layer['transform']['scale_y'], 1.0)

    def test_rig_pinned_foot_stays_with_body_move(self):
        project, bones = self.rigged()
        frame = project['frames'][0]
        report = self.mutate(project, 'rig/solve', {'pose': {'rotations': {}, 'root': [5, 0], 'targets': [
            {'bone': 'c' * 32, 'pin': True}]}})
        target = report['targets'][0]
        self.assertLess(target['residual'], 1.5)
        self.assertAlmostEqual(target['reached'][1], target['target'][1], delta=1.5)

    def test_rig_pose_is_isolated_per_frame(self):
        project, bones = self.rigged()
        first = project['frames'][0]
        second = self.mutate(project, 'frames', {'after': first['id']})['frames'][1]
        project = self.store.get(project['id'])
        untouched = [item['transform'] for item in project['frames'][1]['layers']]
        posed = self.mutate(project, 'rig/pose', {'frame_id': first['id'], 'pose': {'rotations': {'c' * 32: 25}, 'root': [2, 0], 'targets': []}})
        self.assertEqual(untouched, [item['transform'] for item in posed['project']['frames'][1]['layers']])
        second_pose = self.mutate(posed['project'], 'rig/pose', {'frame_id': second['id'], 'pose': {'rotations': {'d' * 32: -25}, 'root': [-2, 0], 'targets': []}})
        self.assertEqual(posed['project']['frames'][0]['layers'], second_pose['project']['frames'][0]['layers'])
        self.assertNotEqual(second_pose['project']['frames'][1]['layers'], untouched)

    def test_rig_preview_matches_saved_render(self):
        import base64
        import io
        project, bones = self.rigged()
        frame = project['frames'][0]
        pose = {'rotations': {}, 'root': [4, 0], 'targets': [{'bone': 'c' * 32, 'pin': True}]}
        saved = self.mutate(project, 'rig/pose', {'frame_id': frame['id'], 'pose': pose})
        stored = saved['project']
        response = self.client.post(self.base(stored) + '/rig/preview', json={'frame_id': frame['id'], 'pose': stored['frames'][0]['pose']})
        self.assertEqual(response.status_code, 200, response.json)
        payload = response.json
        decoded = Image.open(io.BytesIO(base64.b64decode(payload['image'].split(',', 1)[1]))).convert('RGBA')
        expected = render_frame(self.store, stored, frame['id'])
        np.testing.assert_array_equal(np.asarray(decoded), np.asarray(expected))
        self.assertEqual(payload['report']['layers'], [{'bone': item['bone'], 'layer': item['layer'], 'transform': item['transform']} for item in payload['report']['layers']])

    def test_rig_keyframes_are_editable_and_isolated(self):
        project, bones = self.rigged()
        frame = project['frames'][0]
        poses = [{'rotations': {}, 'root': [0, 0], 'targets': []},
                 {'rotations': {}, 'root': [6, -2], 'targets': [{'bone': 'c' * 32, 'pin': True}]}]
        result = self.mutate(project, 'rig/keyframes', {'frame_id': frame['id'], 'poses': poses, 'between': 2, 'duration': 80, 'name': '步态'})
        names = [item['name'] for item in result['frames']]
        self.assertEqual(names[1:], ['步态 1/2', '步态 1-1/2', '步态 1-2/2', '步态 2/2'])
        added = result['frames'][1:]
        self.assertTrue(all(item['pose'] for item in added))
        self.assertEqual(added[0]['duration'], 80)
        self.assertEqual(len({layer['id'] for item in added for layer in item['layers']}), len(added) * len(added[0]['layers']))
        self.assertEqual(sorted(item['pose']['root'][0] for item in added), [0.0, 2.0, 4.0, 6.0])
        self.assertEqual([item['transform'] for item in result['frames'][0]['layers']], [item['transform'] for item in project['frames'][0]['layers']])
        for item in added:
            rendered = render_frame(self.store, result, item['id'])
            self.assertIsNotNone(rendered.getbbox())

    def test_rig_clear_restores_binding_pose(self):
        project, bones = self.rigged()
        frame = project['frames'][0]
        original = [item['transform'] for item in frame['layers']]
        posed = self.mutate(project, 'rig/pose', {'frame_id': frame['id'], 'pose': {'rotations': {'c' * 32: 40}, 'root': [5, 2], 'targets': []}})
        cleared = self.mutate(posed['project'], 'rig/clear', {'frame_id': frame['id']})
        self.assertEqual([item['transform'] for item in cleared['frames'][0]['layers']], original)
        self.assertNotIn('pose', cleared['frames'][0])

    def test_rig_rejects_invalid_hierarchy_and_limits(self):
        project, bones = self.rigged()
        base = self.base(project)
        cyclic = [{'id': 'a' * 32, 'name': 'A', 'parent': 'b' * 32, 'origin': [0, 0], 'angle': 0, 'length': 8},
                  {'id': 'b' * 32, 'name': 'B', 'parent': 'a' * 32, 'origin': [0, 0], 'angle': 0, 'length': 8}]
        response = self.client.put(base + '/rig', json={'revision': project['revision'], 'rig': {'bones': cyclic, 'bindings': []}})
        self.assertEqual(response.status_code, 400)
        invalid_limits = [{'id': 'a' * 32, 'name': 'A', 'parent': None, 'origin': [0, 0], 'angle': 0, 'length': 8, 'limit_min': 30, 'limit_max': -30}]
        response = self.client.put(base + '/rig', json={'revision': project['revision'], 'rig': {'bones': invalid_limits, 'bindings': []}})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(base + '/rig/solve', json={'pose': {'rotations': {'f' * 32: 10}}})
        self.assertEqual(response.status_code, 400)

    def test_rig_exports_match_pose_preview(self):
        import base64
        import io
        project, bones = self.rigged()
        frame = project['frames'][0]
        posed = self.mutate(project, 'rig/pose', {'frame_id': frame['id'], 'pose': {'rotations': {}, 'root': [3, -1], 'targets': []}})
        stored = posed['project']
        data, _ = animated_png(self.store, stored, {})
        with Image.open(io.BytesIO(data)) as decoded:
            preview = self.client.post(self.base(stored) + '/rig/preview', json={'frame_id': frame['id'], 'pose': stored['frames'][0]['pose']}).json
            expected = Image.open(io.BytesIO(base64.b64decode(preview['image'].split(',', 1)[1]))).convert('RGBA')
            np.testing.assert_array_equal(np.asarray(decoded.convert('RGBA')), np.asarray(expected))

    def test_rig_rebases_on_canvas_geometry(self):
        project, bones = self.rigged()
        frame = project['frames'][0]
        posed = self.mutate(project, 'rig/pose', {'frame_id': frame['id'], 'pose': {'rotations': {'c' * 32: 30}, 'root': [3, 0], 'targets': []}})
        cropped = self.mutate(posed['project'], 'crop', {'x': 8, 'y': 8, 'width': 40, 'height': 40})
        self.assertEqual(cropped['rig']['size'], [40, 40])
        self.assertTrue(all('pose' not in item for item in cropped['frames']))
        self.assertTrue(all(binding['bind'] == {'x': 0, 'y': 0, 'scale_x': 1, 'scale_y': 1, 'rotation': 0, 'flip_x': False, 'flip_y': False} for binding in cropped['rig']['bindings']))
        root = [item for item in cropped['rig']['bones'] if item['name'] == '根'][0]
        self.assertEqual(root['origin'], [24.0, 46.0])
        scaled = self.mutate(cropped, 'resize', {'mode': 'scale', 'width': 80, 'height': 80})
        self.assertEqual(scaled['rig']['size'], [80, 80])
        leg = [item for item in scaled['rig']['bones'] if item['name'] == '左腿'][0]
        self.assertEqual(leg['length'], 40.0)
        rotated = self.mutate(scaled, 'rotate', {'turns': 1})
        self.assertEqual(rotated['rig']['size'], [80, 80])
        turned = [item for item in rotated['rig']['bones'] if item['name'] == '左腿'][0]
        self.assertEqual(turned['origin'], [23.0, 42.0])
        self.assertEqual(turned['angle'], 180.0)

    def test_rig_locked_layer_rejects_pose(self):
        project, bones = self.rigged()
        frame = project['frames'][0]
        leg = [item for item in frame['layers'] if item['name'] == '左腿'][0]
        locked = self.mutate(project, 'frames/' + frame['id'] + '/layers/' + leg['id'], {'locked': True}, 'PATCH')
        response = self.client.post(self.base(locked) + '/rig/pose', json={'revision': locked['revision'], 'frame_id': frame['id'], 'pose': {'rotations': {'c' * 32: 30}, 'root': [0, 0], 'targets': []}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.get(locked['id'])['revision'], locked['revision'])
        unchanged = self.mutate(locked, 'rig/pose', {'frame_id': frame['id'], 'pose': {'rotations': {}, 'root': [0, 0], 'targets': []}})
        self.assertEqual([item['transform'] for item in unchanged['project']['frames'][0]['layers']],
                         [item['transform'] for item in project['frames'][0]['layers']])

    def test_rig_removal_keeps_pixels_and_drops_pose(self):
        project, bones = self.rigged()
        frame = project['frames'][0]
        posed = self.mutate(project, 'rig/pose', {'frame_id': frame['id'], 'pose': {'rotations': {'c' * 32: 20}, 'root': [2, 0], 'targets': []}})
        before = render_frame(self.store, posed['project'], frame['id']).tobytes()
        removed = self.mutate(posed['project'], 'rig', {}, 'DELETE')
        self.assertNotIn('rig', removed)
        self.assertTrue(all('pose' not in item for item in removed['frames']))
        self.assertEqual(before, render_frame(self.store, removed, frame['id']).tobytes())
        self.assertEqual(self.client.get(self.base(removed) + '/rig').json['rig'], None)

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
