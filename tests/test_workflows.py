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


    def _stack_project(self, width=40, height=40, background='#3050a0ff'):
        project = self.new(width, height)
        project = self.call('PATCH', self.base(project), {'revision': project['revision'], 'background': background})
        frame = project['frames'][0]
        project = self.mutate(project, 'frames/' + frame['id'] + '/layers', {'name': '上色'})
        frame = project['frames'][0]
        bottom, top = frame['layers'][0]['id'], frame['layers'][1]['id']
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{bottom}/draw', {
            'tool': 'rectangle', 'color': '#3366ccff', 'points': [[0, 0], [12, height - 1]], 'filled': True})
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{top}/draw', {
            'tool': 'rectangle', 'color': '#ffcc33ff', 'points': [[4, 4], [width - 5, height - 5]], 'filled': True})
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{top}', {'blend': 'multiply', 'opacity': .85}, 'PATCH')
        return project, frame['id'], top

    def test_merge_into_group_keeps_blend_backdrop_dependency(self):
        project, frame_id, top = self._stack_project()
        before = np.asarray(render_frame(self.store, project, frame_id))
        with_background = np.asarray(render_frame(self.store, project, frame_id, background=True))
        self.assertFalse(np.array_equal(before, with_background))
        project = self.mutate(project, f'frames/{frame_id}/layers/{top}/merge')
        frame = project['frames'][0]
        self.assertEqual(len(frame['layers']), 1)
        self.assertEqual(frame['layers'][0]['kind'], 'group')
        np.testing.assert_array_equal(before, np.asarray(render_frame(self.store, project, frame_id)))
        np.testing.assert_array_equal(with_background, np.asarray(render_frame(self.store, project, frame_id, background=True)))
        project = self.mutate(project, 'undo')
        project = self.mutate(project, 'redo')
        np.testing.assert_array_equal(with_background, np.asarray(render_frame(self.store, project, frame_id, background=True)))

    def test_group_transform_survives_expand_ungroup_and_reload(self):
        for transform in [
            {'x': 5, 'y': -3},
            {'scale_x': 1.5, 'scale_y': .75},
            {'rotation': 33},
            {'x': 4, 'y': 5, 'rotation': 90, 'scale_x': 1.5, 'scale_y': .5},
            {'x': -2, 'y': 3, 'rotation': -20, 'scale_x': 2, 'scale_y': 2, 'flip_x': True},
        ]:
            project, frame_id, top = self._stack_project()
            project = self.mutate(project, f'frames/{frame_id}/layers/{top}/merge')
            group_id = project['frames'][0]['layers'][0]['id']
            project = self.mutate(project, f'frames/{frame_id}/layers/{group_id}', {'transform': transform}, 'PATCH')
            grouped_image = np.asarray(render_frame(self.store, project, frame_id, background=True))
            check = self.call('POST', self.base(project) + f'/frames/{frame_id}/layers/{group_id}/group-check', {'revision': project['revision'], 'background': True})
            self.assertTrue(check['pixel_identical'])
            self.assertEqual(check['changed_pixels'], 0)
            expanded = self.mutate(project, f'frames/{frame_id}/layers/{group_id}/expand')
            np.testing.assert_array_equal(grouped_image, np.asarray(render_frame(self.store, expanded, frame_id, background=True)))
            project = self.mutate(expanded, 'undo')
            ungrouped = self.mutate(project, f'frames/{frame_id}/layers/{group_id}/ungroup')
            np.testing.assert_array_equal(grouped_image, np.asarray(render_frame(self.store, ungrouped, frame_id, background=True)))
            persisted = create_app(self.temp.name)
            try:
                stored = persisted.extensions['store'].get(project['id'])
                restored = render_frame(persisted.extensions['store'], stored, frame_id, background=True)
                np.testing.assert_array_equal(grouped_image, np.asarray(restored))
            finally:
                persisted.extensions['jobs'].executor.shutdown(wait=True)

    def test_group_expand_keeps_pixels_clipped_outside_intermediate_canvas(self):
        project, frame_id, top = self._stack_project()
        project = self.mutate(project, f'frames/{frame_id}/layers/{top}', {
            'transform': {'x': 20, 'scale_x': 1.6, 'scale_y': 1.6}}, 'PATCH')
        project = self.mutate(project, f'frames/{frame_id}/layers/{top}/merge')
        group_id = project['frames'][0]['layers'][-1]['id']
        project = self.mutate(project, f'frames/{frame_id}/layers/{group_id}', {'transform': {'x': -20, 'y': 5}}, 'PATCH')
        rendered = np.asarray(render_frame(self.store, project, frame_id))
        self.assertGreater(int(np.count_nonzero(rendered[:, :, 3]),), 0)
        expanded = self.mutate(project, f'frames/{frame_id}/layers/{group_id}/expand')
        np.testing.assert_array_equal(rendered, np.asarray(render_frame(self.store, expanded, frame_id)))

    def test_group_child_edits_still_change_result_after_expand(self):
        project, frame_id, top = self._stack_project()
        project = self.mutate(project, f'frames/{frame_id}/layers/{top}/merge')
        group_id = project['frames'][0]['layers'][0]['id']
        project = self.mutate(project, f'frames/{frame_id}/layers/{group_id}', {
            'transform': {'x': 6, 'y': -4, 'rotation': 25, 'scale_x': 1.4, 'scale_y': .8}}, 'PATCH')
        before = np.asarray(render_frame(self.store, project, frame_id))
        expanded = self.mutate(project, f'frames/{frame_id}/layers/{group_id}/expand')
        children = [layer for layer in expanded['frames'][0]['layers'] if layer.get('kind') != 'group']
        self.assertTrue(all('affine' in layer for layer in children))
        target = next(layer for layer in children if layer['name'] == '上色')
        self.assertEqual(target['blend'], 'multiply')
        baked = self.mutate(expanded, f'frames/{frame_id}/layers/{target["id"]}/bake')
        edited = self.mutate(baked, f'frames/{frame_id}/layers/{target["id"]}/draw', {
            'tool': 'rectangle', 'color': '#ff0000ff', 'points': [[2, 2], [9, 9]], 'filled': True})
        after = np.asarray(render_frame(self.store, edited, frame_id))
        self.assertFalse(np.array_equal(before, after))
        reloaded = create_app(self.temp.name)
        try:
            stored = reloaded.extensions['store'].get(project['id'])
            np.testing.assert_array_equal(after, np.asarray(render_frame(reloaded.extensions['store'], stored, frame_id)))
        finally:
            reloaded.extensions['jobs'].executor.shutdown(wait=True)

    def test_nested_groups_keep_coordinates_and_compare(self):
        project, frame_id, top = self._stack_project()
        project = self.mutate(project, f'frames/{frame_id}/layers/{top}/merge')
        inner_id = project['frames'][0]['layers'][-1]['id']
        project = self.mutate(project, f'frames/{frame_id}/layers', {'name': '底图'})
        base = project['frames'][0]['layers'][-1]['id']
        project = self.mutate(project, f'frames/{frame_id}/layers/{base}/draw', {
            'tool': 'rectangle', 'color': '#22aa44ff', 'points': [[0, 0], [15, 15]], 'filled': True})
        project = self.mutate(project, f'frames/{frame_id}/layers/{base}', {'blend': 'screen', 'opacity': .6}, 'PATCH')
        project = self.mutate(project, f'frames/{frame_id}/layers/{base}/merge')
        outer_id = project['frames'][0]['layers'][-1]['id']
        self.assertEqual(project['frames'][0]['layers'][0]['kind'], 'group')
        self.assertEqual(project['frames'][0]['layers'][0]['children'][0]['kind'], 'group')
        self.assertEqual(project['frames'][0]['layers'][0]['children'][0]['id'], inner_id)
        project = self.mutate(project, f'frames/{frame_id}/layers/{outer_id}', {
            'transform': {'x': -2, 'y': 3, 'rotation': -12, 'scale_x': .9, 'scale_y': 1.4, 'flip_x': True}}, 'PATCH')
        reference = np.asarray(render_frame(self.store, project, frame_id, background=True))
        for group_id in (outer_id, inner_id):
            check = self.call('POST', self.base(project) + f'/frames/{frame_id}/layers/{group_id}/group-check', {'revision': project['revision'], 'background': True})
            self.assertTrue(check['pixel_identical'], check)
        expanded = self.mutate(project, f'frames/{frame_id}/layers/{outer_id}/expand')
        np.testing.assert_array_equal(reference, np.asarray(render_frame(self.store, expanded, frame_id, background=True)))
        nested = next(layer for layer in expanded['frames'][0]['layers'] if layer.get('kind') == 'group')
        ungrouped = self.mutate(expanded, f'frames/{frame_id}/layers/{nested["id"]}/ungroup')
        np.testing.assert_array_equal(reference, np.asarray(render_frame(self.store, ungrouped, frame_id, background=True)))

    def test_transformed_group_blocks_child_pixel_edits_until_expanded(self):
        project, frame_id, top = self._stack_project()
        project = self.mutate(project, f'frames/{frame_id}/layers/{top}/merge')
        group_id = project['frames'][0]['layers'][0]['id']
        child = project['frames'][0]['layers'][0]['children'][1]
        identity_draw = self.mutate(project, f'frames/{frame_id}/layers/{child["id"]}/draw', {
            'tool': 'pencil', 'color': '#ff0000ff', 'points': [[4, 4]]})
        self.assertEqual(identity_draw['revision'], project['revision'] + 1)
        project = self.mutate(identity_draw, f'frames/{frame_id}/layers/{group_id}', {'transform': {'x': 5, 'rotation': 30, 'scale_x': 1.5, 'scale_y': .6}}, 'PATCH')
        response = self.client.post(self.base(project) + f'/frames/{frame_id}/layers/{child["id"]}/draw', json={'revision': project['revision'], 'tool': 'pencil', 'points': [[4, 4]]})
        self.assertEqual(response.status_code, 400)
        response.close()
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])
        expanded = self.mutate(project, f'frames/{frame_id}/layers/{group_id}/expand')
        target = next(layer for layer in expanded['frames'][0]['layers'] if layer['name'] == '上色')
        baked = self.mutate(expanded, f'frames/{frame_id}/layers/{target["id"]}/bake')
        drawn = self.mutate(baked, f'frames/{frame_id}/layers/{target["id"]}/draw', {'tool': 'rectangle', 'color': '#ff0000ff', 'points': [[3, 3], [10, 10]], 'filled': True})
        self.assertEqual(drawn['revision'], baked['revision'] + 1)

    def test_group_rendering_matches_ungrouped_stack_for_every_blend(self):
        project = self.new(32, 32)
        frame = project['frames'][0]
        for index in range(3):
            project = self.mutate(project, 'frames/' + frame['id'] + '/layers', {'name': f'层{index}'})
        frame = project['frames'][0]
        layers = frame['layers']
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{layers[0]["id"]}/draw', {'tool': 'rectangle', 'color': '#2244aaff', 'points': [[4, 4], [31, 20]], 'filled': True})
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{layers[1]["id"]}/draw', {'tool': 'rectangle', 'color': '#66cc33ff', 'points': [[12, 8], [27, 27]], 'filled': True})
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{layers[2]["id"]}/draw', {'tool': 'rectangle', 'color': '#ff33aaff', 'points': [[0, 0], [15, 15]], 'filled': True})
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{layers[1]["id"]}', {'blend': 'screen', 'opacity': .7}, 'PATCH')
        flat = np.asarray(render_frame(self.store, project, frame['id']))
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{layers[2]["id"]}/merge')
        project = self.mutate(project, f'frames/{frame["id"]}/layers/{project["frames"][0]["layers"][-1]["id"]}/merge')
        group_id = project['frames'][0]['layers'][-1]['id']
        grouped = np.asarray(render_frame(self.store, project, frame['id']))
        np.testing.assert_array_equal(flat, grouped)
        ungrouped = self.mutate(project, f'frames/{frame["id"]}/layers/{group_id}/ungroup')
        np.testing.assert_array_equal(flat, np.asarray(render_frame(self.store, ungrouped, frame['id'])))


if __name__ == '__main__':
    unittest.main(verbosity=2)
