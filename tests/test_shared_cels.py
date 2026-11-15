import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flipbook import create_app
from flipbook.composite import render_frame, transform_layer
from flipbook.cels import _source_lookup


class SharedCelWorkflows(unittest.TestCase):
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
        self.assertEqual(response.status_code, expected,
                         response.json if response.is_json else response.data[:200])
        result = response.json if response.is_json else response.data
        response.close()
        return result

    def base(self, project):
        return '/api/projects/' + project['id']

    def mutate(self, project, path, data=None, method='POST', expected=200):
        payload = {'revision': project['revision']}
        payload.update(data or {})
        return self.call(method, self.base(project) + '/' + path, payload, expected)

    def layer(self, project, frame_index=0, layer_index=0):
        frame = project['frames'][frame_index]
        return frame['id'], frame['layers'][layer_index]['id']

    def draw_path(self, project, frame_index=0, layer_index=0):
        frame_id, layer_id = self.layer(project, frame_index, layer_index)
        return f'frames/{frame_id}/layers/{layer_id}/draw'

    def layer_path(self, project, frame_index=0, layer_index=0, suffix=''):
        frame_id, layer_id = self.layer(project, frame_index, layer_index)
        path = f'frames/{frame_id}/layers/{layer_id}'
        return path + ('/' + suffix if suffix else '')

    def two_frame_cel(self, width=32, height=32):
        project = self.call('POST', '/api/projects',
                            {'name': '共用原画', 'width': width, 'height': height}, 201)
        project = self.mutate(project, 'frames', {'after': project['frames'][0]['id']})
        self.assertEqual(len(project['frames']), 2)
        f0, l0 = self.layer(project, 0)
        f1, l1 = self.layer(project, 1)
        project = self.mutate(project, 'cels/link', {
            'source_frame': f0, 'source_layer': l0,
            'targets': [{'frame_id': f1, 'layer_id': l1}],
        })
        return project

    def rendered(self, project, frame_index):
        return render_frame(self.store, project, project['frames'][frame_index]['id'])

    def test_draw_on_transformed_member_keeps_independent_transforms(self):
        project = self.two_frame_cel()
        # Give each member its own transform / opacity / blend.
        project = self.mutate(project, self.layer_path(project, 0), {
            'transform': {'x': 4, 'y': 0, 'scale_x': 1, 'scale_y': 1,
                          'rotation': 0, 'flip_x': False, 'flip_y': False}}, 'PATCH')
        project = self.mutate(project, self.layer_path(project, 1), {
            'transform': {'x': 0, 'y': 0, 'scale_x': 1, 'scale_y': 1,
                          'rotation': 0, 'flip_x': False, 'flip_y': False},
            'opacity': 0.5, 'blend': 'multiply'}, 'PATCH')
        transform_before = project['frames'][0]['layers'][0]['transform']
        other_before = project['frames'][1]['layers'][0]['transform']

        # Draw through the translated first member at a visible display pixel.
        project = self.mutate(project, self.draw_path(project, 0), {
            'tool': 'pencil', 'color': '#ff0000ff', 'points': [[10, 10]], 'size': 1})

        first = project['frames'][0]['layers'][0]
        second = project['frames'][1]['layers'][0]
        # Both members still point at the same shared pixels ...
        self.assertEqual(first['asset'], second['asset'])
        # ... while every instance property stays independent and untouched.
        self.assertEqual(first['transform'], transform_before)
        self.assertEqual(second['transform'], other_before)
        self.assertEqual(second['opacity'], 0.5)
        self.assertEqual(second['blend'], 'multiply')
        # The stroke landed in source space: display (10,10) with x=4 maps
        # to source (6,10).
        asset = self.store.image(project['id'], first['asset'])
        self.assertEqual(asset.getpixel((6, 10)), (255, 0, 0, 255))
        self.assertEqual(asset.getpixel((10, 10))[3], 0)
        # And it shows up on both members at their own transformed positions;
        # the second member keeps its independent 50% opacity.
        self.assertEqual(self.rendered(project, 0).getpixel((10, 10)), (255, 0, 0, 255))
        self.assertEqual(self.rendered(project, 1).getpixel((6, 10)), (255, 0, 0, 128))

    def test_shrunk_member_edits_do_not_lose_source_pixels(self):
        # A 5x5 red block drawn at source resolution, then viewed at half size.
        project = self.two_frame_cel()
        project = self.mutate(project, self.draw_path(project, 0), {
            'tool': 'rectangle', 'color': '#ff0000ff',
            'points': [[8, 8], [12, 12]], 'filled': True})
        project = self.mutate(project, self.layer_path(project, 1), {
            'transform': {'x': 0, 'y': 0, 'scale_x': 0.5, 'scale_y': 0.5,
                          'rotation': 0, 'flip_x': False, 'flip_y': False}}, 'PATCH')
        source_before = np.asarray(self.store.image(
            project['id'], project['frames'][0]['layers'][0]['asset'])).copy()
        self.assertEqual(np.count_nonzero(source_before[:, :, 3]), 25)

        # Paint on the half-size instance at a display pixel that visibly
        # covers the block (half-scale NEAREST keeps odd source coordinates).
        project = self.mutate(project, self.draw_path(project, 1), {
            'tool': 'pencil', 'color': '#00ff00ff', 'points': [[19, 19]], 'size': 1})
        asset = self.store.image(project['id'], project['frames'][0]['layers'][0]['asset'])
        array = np.asarray(asset)
        # All 25 original cells survive; the new green cell is added once,
        # and no source pixel is dropped by the member's half-size display.
        self.assertEqual(np.count_nonzero(array[:, :, 3]), 26)
        self.assertEqual(asset.getpixel((23, 23)), (0, 255, 0, 255))
        self.assertEqual(asset.getpixel((8, 8)), (255, 0, 0, 255))
        self.assertEqual(asset.getpixel((12, 12)), (255, 0, 0, 255))

    def test_visible_display_position_maps_to_displayed_source_pixel(self):
        project = self.two_frame_cel()
        project = self.mutate(project, self.draw_path(project, 0), {
            'tool': 'rectangle', 'color': '#ff0000ff',
            'points': [[8, 8], [12, 12]], 'filled': True})
        transform = {'x': 3, 'y': -4, 'scale_x': 1.5, 'scale_y': 0.75,
                     'rotation': 37, 'flip_x': True, 'flip_y': False}
        project = self.mutate(project, self.layer_path(project, 1),
                              {'transform': transform}, 'PATCH')
        size = (project['width'], project['height'])
        source = self.store.image(project['id'],
                                  project['frames'][0]['layers'][0]['asset'])
        u, v, valid = _source_lookup(transform, size, size)
        displayed = transform_layer(source, transform, size)
        # Every visible display position resolves to exactly the source pixel
        # being displayed there, so clicking it has one consistent meaning.
        for y in range(size[1]):
            for x in range(size[0]):
                if not valid[y, x]:
                    self.assertEqual(displayed.getpixel((x, y))[3], 0)
                else:
                    self.assertEqual(source.getpixel((int(u[y, x]), int(v[y, x]))),
                                     displayed.getpixel((x, y)))
        # And an API stroke at a visible position lands on that source pixel.
        target = next((x, y) for y in range(size[1]) for x in range(size[0])
                      if valid[y, x] and displayed.getpixel((x, y))[3] > 0)
        project = self.mutate(project, self.draw_path(project, 1), {
            'tool': 'pencil', 'color': '#00ff00ff',
            'points': [[target[0], target[1]]], 'size': 1})
        asset = self.store.image(project['id'],
                                 project['frames'][0]['layers'][0]['asset'])
        su, sv = int(u[target[1], target[0]]), int(v[target[1], target[0]])
        self.assertEqual(asset.getpixel((su, sv)), (0, 255, 0, 255))

    def test_erase_on_scaled_member_edits_shared_source(self):
        project = self.two_frame_cel()
        project = self.mutate(project, self.draw_path(project, 0), {
            'tool': 'rectangle', 'color': '#ff0000ff',
            'points': [[8, 8], [12, 12]], 'filled': True})
        project = self.mutate(project, self.layer_path(project, 1), {
            'transform': {'x': 0, 'y': 0, 'scale_x': 2, 'scale_y': 2,
                          'rotation': 0, 'flip_x': False, 'flip_y': False}}, 'PATCH')
        # Erase at display (4,4), which the 2x member maps to source (10,10).
        project = self.mutate(project, self.draw_path(project, 1), {
            'tool': 'erase', 'points': [[4, 4]], 'size': 1})
        asset = self.store.image(project['id'], project['frames'][0]['layers'][0]['asset'])
        self.assertEqual(asset.getpixel((10, 10))[3], 0)
        self.assertEqual(asset.getpixel((9, 10))[3], 255)
        # Shared: the first (unscaled) member lost the same pixel.
        self.assertEqual(self.rendered(project, 0).getpixel((10, 10))[3], 0)

    def test_detach_makes_member_independent(self):
        project = self.two_frame_cel()
        project = self.mutate(project, self.draw_path(project, 0), {
            'tool': 'pencil', 'color': '#ff0000ff', 'points': [[5, 5]], 'size': 1})
        f1, l1 = self.layer(project, 1)
        project = self.mutate(project, f'frames/{f1}/layers/{l1}/cel-detach')
        detached = project['frames'][1]['layers'][0]
        shared = project['frames'][0]['layers'][0]
        self.assertNotIn('cel_id', detached)
        self.assertTrue(shared.get('cel_id'))
        # Drawing on the detached layer no longer touches the shared asset.
        project = self.mutate(project, self.draw_path(project, 1), {
            'tool': 'pencil', 'color': '#00ff00ff', 'points': [[6, 6]], 'size': 1})
        detached = project['frames'][1]['layers'][0]
        shared = project['frames'][0]['layers'][0]
        self.assertNotEqual(detached['asset'], shared['asset'])
        self.assertEqual(self.rendered(project, 0).getpixel((6, 6))[3], 0)
        self.assertEqual(self.rendered(project, 1).getpixel((6, 6)), (0, 255, 0, 255))

    def test_undo_redo_and_reopen_preserve_shared_relationship(self):
        project = self.two_frame_cel()
        project = self.mutate(project, self.layer_path(project, 1), {
            'transform': {'x': 2, 'y': 0, 'scale_x': 0.5, 'scale_y': 0.5,
                          'rotation': 0, 'flip_x': False, 'flip_y': False}}, 'PATCH')
        blank = np.asarray(self.rendered(project, 0)).copy()
        project = self.mutate(project, self.draw_path(project, 1), {
            'tool': 'pencil', 'color': '#ff0000ff', 'points': [[18, 16]], 'size': 1})
        painted_asset = project['frames'][0]['layers'][0]['asset']
        self.assertEqual(project['frames'][1]['layers'][0]['asset'], painted_asset)
        # Undo restores both members and keeps them linked with transforms.
        project = self.mutate(project, 'undo')
        self.assertEqual(project['frames'][0]['layers'][0]['asset'],
                         project['frames'][1]['layers'][0]['asset'])
        np.testing.assert_array_equal(np.asarray(self.rendered(project, 0)), blank)
        self.assertEqual(project['frames'][1]['layers'][0]['transform']['scale_x'], 0.5)
        # Redo brings the shared edit back.
        project = self.mutate(project, 'redo')
        self.assertEqual(project['frames'][1]['layers'][0]['asset'], painted_asset)
        # Reopen the project from disk: relationship still intact.
        reloaded = self.call('GET', self.base(project))
        self.assertEqual(reloaded['frames'][0]['layers'][0]['cel_id'],
                         reloaded['frames'][1]['layers'][0]['cel_id'])
        self.assertEqual(reloaded['frames'][0]['layers'][0]['asset'],
                         reloaded['frames'][1]['layers'][0]['asset'])
        self.assertEqual(reloaded['frames'][1]['layers'][0]['transform']['x'], 2)

    def test_locked_member_and_unsupported_tool_rejected_without_revision_change(self):
        project = self.two_frame_cel()
        project = self.mutate(project, self.layer_path(project, 1), {'locked': True}, 'PATCH')
        revision = project['revision']
        self.mutate(project, self.draw_path(project, 0), {
            'tool': 'pencil', 'color': '#ff0000ff', 'points': [[1, 1]], 'size': 1},
            expected=400)
        self.assertEqual(self.store.get(project['id'])['revision'], revision)
        project = self.mutate(project, self.layer_path(project, 1), {'locked': False}, 'PATCH')
        self.mutate(project, self.draw_path(project, 0), {
            'tool': 'fill', 'color': '#ff0000ff', 'points': [[1, 1]]}, expected=400)
        self.assertEqual(self.store.get(project['id'])['revision'], revision + 1)

    def test_stroke_outside_transformed_area_is_ignored(self):
        project = self.two_frame_cel()
        project = self.mutate(project, self.layer_path(project, 1), {
            'transform': {'x': 0, 'y': 0, 'scale_x': 0.5, 'scale_y': 0.5,
                          'rotation': 0, 'flip_x': False, 'flip_y': False}}, 'PATCH')
        before = project['frames'][0]['layers'][0]['asset']
        # (0,0) on the half-size instance is empty padding.
        project = self.mutate(project, self.draw_path(project, 1), {
            'tool': 'pencil', 'color': '#ff0000ff', 'points': [[0, 0]], 'size': 1})
        self.assertEqual(project['frames'][0]['layers'][0]['asset'], before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
