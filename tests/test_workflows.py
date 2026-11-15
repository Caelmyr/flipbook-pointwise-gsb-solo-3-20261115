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

    def start_floating(self, project, body, layer_id=None, frame=None):
        frame = frame or project['frames'][0]
        layer_id = layer_id or frame['layers'][0]['id']
        return self.call('POST', self.base(project) + '/frames/' + frame['id'] + '/layers/' + layer_id + '/floating',
                         {'revision': project['revision'], **body}, 201)

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

    def floating_path(self, project, suffix=''):
        return self.layer_path(project, 'floating') + ('/' + suffix if suffix else '')

    def floating_image(self, project, session_id, suffix='preview'):
        data = self.call('GET', self.base(project) + '/' + self.floating_path(project, session_id + '/' + suffix))
        with Image.open(io.BytesIO(data)) as decoded:
            return np.asarray(decoded.convert('RGBA')).copy()

    def test_floating_tryout_never_touches_project_revision(self):
        project = self.paint(self.new(width=24, height=24), '#ff000080', [[2, 2], [9, 9]])
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 2, 'y': 2, 'width': 8, 'height': 8})
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])
        start = self.floating_image(project, session['id'])
        self.assertEqual(start[4, 4].tolist(), [255, 0, 0, 128])
        path = self.base(project) + '/' + self.floating_path(project, session['id'])
        for transform in [{'dx': 12}, {'rotation': 37}, {'scale_x': 1.6, 'scale_y': .6}]:
            session = self.call('PATCH', path, transform)
        session = self.call('PATCH', path, {'x': 0, 'y': 0, 'rotation': 0, 'scale_x': 1, 'scale_y': 1})
        np.testing.assert_array_equal(self.floating_image(project, session['id']), np.asarray(self.image(project)))
        self.assertEqual(self.store.get(project['id'])['revision'], project['revision'])
        self.assertEqual(len(list(self.store.directory(project['id']).glob('floating/*.json'))), 1)

    def test_floating_move_off_canvas_and_back(self):
        project = self.paint(self.new(width=24, height=24), '#ff0000a0', [[3, 3], [10, 10]])
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 3, 'y': 3, 'width': 8, 'height': 8})
        path = self.base(project) + '/' + self.floating_path(project, session['id'])
        self.call('PATCH', path, {'dx': 400, 'dy': -300})
        off = self.floating_image(project, session['id'])
        self.assertEqual(int(off[:, :, 3].max()), 0)
        self.call('PATCH', path, {'dx': 0, 'dy': 0})
        np.testing.assert_array_equal(self.floating_image(project, session['id']), np.asarray(self.image(project)))

    def test_lasso_add_subtract_and_soft_edge(self):
        project = self.paint(self.new(width=24, height=24), '#3388ff80', [[1, 1], [14, 14]])
        lasso = [[1, 1], [14, 1], [14, 14], [1, 14]]
        session = self.start_floating(project, {'shape': 'lasso', 'points': lasso})
        path = self.base(project) + '/' + self.floating_path(project, session['id'])
        session = self.call('POST', path + '/mask', {'operation': 'subtract', 'feather': 2.5, 'x': 1, 'y': 1, 'width': 6, 'height': 6})
        self.assertLess(session['effective_coverage'], session['masked_pixels'])
        self.assertEqual(len(session['selection']), 2)
        self.assertEqual(session['selection'][1]['feather'], 2.5)
        session = self.call('POST', path + '/mask', {'operation': 'add', 'shape': 'ellipse', 'x': 12, 'y': 1, 'width': 8, 'height': 8})
        soft = self.floating_image(project, session['id'])
        feather_values = np.asarray(self._masked_alpha(project, session['id']))
        self.assertTrue(np.any((feather_values > 0) & (feather_values < 255)))
        self.call('PATCH', path, {'dx': 5})
        moved = self.floating_image(project, session['id'])
        self.assertGreater(int(moved[4, 9, 3]), 0)

    def _masked_alpha(self, project, session_id):
        data = self.call('GET', self.base(project) + '/' + self.floating_path(project, session_id + '/mask.png'))
        with Image.open(io.BytesIO(data)) as decoded:
            return np.asarray(decoded.convert('L')).copy()

    def test_floating_overlap_does_not_darken_soft_edge(self):
        project = self.paint(self.new(width=24, height=24), '#40a06080', [[2, 2], [10, 10]])
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 2, 'y': 2, 'width': 9, 'height': 9, 'feather': 1.5})
        path = self.base(project) + '/' + self.floating_path(project, session['id'])
        original_peak = int(self.floating_image(project, session['id'])[:, :, 3].max())
        for dx in (1, 2, 3, 4):
            self.call('PATCH', path, {'dx': dx})
            overlap = self.floating_image(project, session['id'])
            self.assertLessEqual(int(overlap[:, :, 3].max()), original_peak)
        self.call('PATCH', path, {'dx': 0, 'dy': 0})
        np.testing.assert_array_equal(self.floating_image(project, session['id']), np.asarray(self.image(project)))

    def test_floating_copy_keeps_source_pixels(self):
        project = self.paint(self.new(width=24, height=24), '#ff8800a0', [[3, 3], [8, 8]])
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 3, 'y': 3, 'width': 6, 'height': 6, 'copy': True})
        path = self.base(project) + '/' + self.floating_path(project, session['id'])
        self.assertTrue(session['copy'])
        self.call('PATCH', path, {'dx': 10})
        copied = self.floating_image(project, session['id'])
        self.assertEqual(copied[4, 4].tolist(), [255, 136, 0, 160])
        self.assertEqual(copied[4, 14].tolist(), [255, 136, 0, 160])

    def test_floating_preview_matches_confirm_and_single_undo(self):
        project = self.paint(self.new(width=24, height=24), '#ff8800a0', [[3, 3], [10, 10]])
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 3, 'y': 3, 'width': 8, 'height': 8})
        path = self.base(project) + '/' + self.floating_path(project, session['id'])
        self.call('PATCH', path, {'x': 9, 'y': -2, 'rotation': 40, 'scale_x': 1.4, 'scale_y': 1.4})
        preview = self.floating_image(project, session['id'])
        undo_before = list(self.store.get(project['id'])['undo'])
        project = self.call('POST', path + '/confirm', {'revision': project['revision']})
        self.assertEqual(len(self.store.get(project['id'])['undo']), len(undo_before) + 1)
        self.assertEqual(self.store.history(project['id'])[0]['operation'], '确认浮动选区')
        np.testing.assert_array_equal(np.asarray(self.image(project)), preview)
        self.assertEqual(list(self.store.directory(project['id']).glob('floating/*.json')), [])
        project = self.mutate(project, 'undo')
        self.assertEqual(self.image(project).getpixel((4, 4)), (255, 136, 0, 160))
        self.assertEqual(self.image(project).getpixel((14, 4))[3], 0)

    def test_floating_cancel_restores_and_stale_base_is_rejected(self):
        project = self.paint(self.new(width=24, height=24), '#0000ffa0', [[2, 2], [9, 9]])
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 2, 'y': 2, 'width': 7, 'height': 7})
        path = self.base(project) + '/' + self.floating_path(project, session['id'])
        self.call('PATCH', path, {'dx': 6})
        cancelled = self.call('DELETE', path, {'revision': project['revision']})
        self.assertEqual(cancelled['revision'], project['revision'])
        self.assertEqual(self.image(cancelled).getpixel((4, 4)), (0, 0, 255, 160))
        response = self.client.get(path + '/preview')
        self.assertEqual(response.status_code, 400)
        response.close()
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 2, 'y': 2, 'width': 7, 'height': 7})
        stale = self.paint(project, '#ffffff40', [[18, 18], [20, 20]])
        response = self.client.get(self.base(stale) + '/' + self.floating_path(stale, session['id'] + '/preview'))
        self.assertEqual(response.status_code, 409)
        response.close()

    def test_floating_preview_composites_lower_layer(self):
        project = self.paint(self.new(width=16, height=16), '#0000ffff', [[0, 0], [15, 15]])
        frame_id = project['frames'][0]['id']
        project = self.mutate(project, 'frames/' + frame_id + '/layers', {'name': '浮动层', 'color': '#00000000'})
        layer_id = project['frames'][0]['layers'][-1]['id']
        project = self.mutate(project, 'frames/' + frame_id + '/layers/' + layer_id + '/draw', {'tool': 'rectangle', 'points': [[4, 4], [7, 7]], 'color': '#8800ffa0', 'filled': True})
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 4, 'y': 4, 'width': 4, 'height': 4}, layer_id, project['frames'][0])
        path = self.base(project) + '/frames/' + frame_id + '/layers/' + layer_id + '/floating/' + session['id']
        self.call('PATCH', path, {'dx': 5})
        data = self.call('GET', path + '/preview?frame=true')
        with Image.open(io.BytesIO(data)) as decoded:
            frame = np.asarray(decoded.convert('RGBA'))
        self.assertEqual(frame[4, 4].tolist(), [0, 0, 255, 255])
        self.assertEqual(frame[5, 10].tolist(), [85, 0, 255, 255])
        self.assertEqual(frame[5, 4].tolist(), [0, 0, 255, 255])

    def test_floating_session_survives_restart_and_guards_basis(self):
        project = self.paint(self.new(width=24, height=24), '#aa44ff80', [[2, 2], [10, 10]])
        frame_id = project['frames'][0]['id']
        layer_id = project['frames'][0]['layers'][0]['id']
        session = self.start_floating(project, {'shape': 'rectangle', 'x': 2, 'y': 2, 'width': 9, 'height': 9})
        path = self.base(project) + '/frames/' + frame_id + '/layers/' + layer_id + '/floating/' + session['id']
        self.call('PATCH', path, {'x': 5, 'rotation': 22})
        another = create_app(self.temp.name)
        try:
            client = another.test_client()
            sessions = client.get(self.base(project) + '/floating').json
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]['transform']['x'], 5)
            moved = client.get(path + '/preview').data
            with Image.open(io.BytesIO(moved)) as decoded:
                self.assertEqual(decoded.size, (24, 24))
            response = client.post(path + '/confirm', json={'revision': project['revision'] - 1})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(client.get(self.base(project) + '/floating').json[0]['id'], session['id'])
            response = client.post(path + '/confirm', json={'revision': project['revision']})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(client.get(self.base(project) + '/floating').json, [])
        finally:
            another.extensions['jobs'].executor.shutdown(wait=True)
        project = self.store.get(project['id'])
        pending = self.start_floating(project, {'shape': 'rectangle', 'x': 2, 'y': 2, 'width': 6, 'height': 6})
        other_layer = self.base(project) + '/frames/' + frame_id + '/layers/' + 'f' * 32 + '/floating/' + pending['id']
        response = self.client.get(other_layer + '/preview')
        self.assertEqual(response.status_code, 400)
        response.close()
        fresh = self.store.get(project['id'])
        self.mutate(fresh, self.layer_path(fresh), {'locked': True}, 'PATCH')
        response = self.client.get(self.base(project) + '/' + self.floating_path(project, pending['id'] + '/preview'))
        self.assertEqual(response.status_code, 400)
        response.close()

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
