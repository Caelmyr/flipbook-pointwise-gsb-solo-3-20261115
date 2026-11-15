import io
import json
from flask import Blueprint, request, jsonify, send_file, Response
from PIL import Image
from . import validate as v
from .model import find_frame, find_layer
from .composite import render_frame, onion_skin, preview, png_bytes, difference
from .importing import import_into_project, import_new_project, slice_sheet, import_project_bundle
from .atlas_import import import_atlas
from .atlas import build_atlas
from .analysis import animation_report, frame_metrics, seam_metrics, loop_seam, repeat_preview, alpha_collision, motion_contact_sheet, duplicate_cleanup
from .geometry import resize_canvas, crop_all, trim_all, align_content, rotate_canvas
from .exporting import json_bytes, timeline_csv, tiled_bundle
from .pipelines import effect_pipeline, motion_trail, typewriter, radial_burst, temporal_average
from .ninepatch import ninepatch_project, seamless_frames
from .contracts import check_assets, export_preflight
from .reports import project_report
from .presets import create_demo
from .palettes import ramp, color_inventory


def body():
    value = request.get_json(silent=True)
    if value is None:
        raise v.Invalid('请提交JSON参数')
    return v.object_value(value)


def image_response(image):
    return Response(png_bytes(image), mimetype='image/png', headers={'Cache-Control': 'no-store'})


def download(data, name, mimetype):
    return send_file(io.BytesIO(data), mimetype=mimetype, as_attachment=True, download_name=name)


def uploaded(name='file'):
    result = request.files.get(name)
    if result is None:
        raise v.Invalid('请选择文件：' + name)
    return result


def selected(data, project):
    return data.get('frame_ids', [f['id'] for f in project['frames']])


def create_api(store, layers, timeline, tween, palettes, tilemaps, jobs, recipes):
    api = Blueprint('api', __name__, url_prefix='/api')

    @api.get('/health')
    def health():
        return jsonify({'application': 'Flipbook Studio', 'version': '1.0', 'status': 'ok'})

    @api.get('/catalog')
    def catalog():
        from .catalog import catalog
        return jsonify(catalog())

    @api.get('/projects')
    def projects():
        return jsonify(store.list(request.args.get('query', ''), request.args.get('archived') == 'true'))

    @api.post('/projects')
    def create_project():
        data = body()
        width, height = v.dimensions(data)
        return jsonify(store.create(data.get('name', '未命名动画'), width, height)), 201

    @api.post('/demo')
    def demo():
        data = body()
        return jsonify(create_demo(store, data.get('kind', 'robot'))), 201

    @api.post('/import')
    def import_project():
        file = uploaded()
        return jsonify(import_new_project(store, file.filename, file.stream)), 201

    @api.post('/import-bundle')
    def import_bundle():
        return jsonify(import_project_bundle(store, uploaded().stream)), 201

    @api.post('/import-atlas')
    def import_atlas_files():
        file = uploaded('image')
        metadata_file = uploaded('metadata')
        try:
            metadata = json.load(metadata_file.stream)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise v.Invalid('图集元数据JSON无效') from error
        return jsonify(import_atlas(store, file.stream, metadata)), 201

    @api.get('/projects/<key>')
    def project(key):
        return jsonify(store.get(key))

    @api.patch('/projects/<key>')
    def update_project(key):
        data = body()
        return jsonify(store.update(key, v.revision(data), data))

    @api.post('/projects/<key>/duplicate')
    def duplicate_project(key):
        return jsonify(store.duplicate(key)), 201

    @api.get('/projects/<key>/history')
    def history(key):
        return jsonify(store.history(key))

    @api.post('/projects/<key>/undo')
    def undo(key):
        data = body()
        return jsonify(store.undo(key, v.revision(data)))

    @api.post('/projects/<key>/redo')
    def redo(key):
        data = body()
        return jsonify(store.redo(key, v.revision(data)))

    @api.get('/projects/<key>/inventory')
    def inventory(key):
        return jsonify(store.asset_inventory(key))

    @api.get('/projects/<key>/integrity')
    def integrity(key):
        with store.lock:
            project = store.get(key)
            return jsonify(check_assets(store, project))

    @api.get('/projects/<key>/frames/<frame_id>/image')
    def frame_image(key, frame_id):
        with store.lock:
            project = store.get(key)
            previous = v.integer(request.args.get('previous', 0), '前方洋葱帧数', 0, 5)
            following = v.integer(request.args.get('following', 0), '后方洋葱帧数', 0, 5)
            image = onion_skin(store, project, frame_id, previous, following) if previous or following else render_frame(store, project, frame_id)
            zoom = v.integer(request.args.get('zoom', 1), '放大倍率', 1, 16)
            checker = request.args.get('checker') == 'true'
            grid = request.args.get('grid') == 'true'
            if checker or grid or zoom != 1:
                image = preview(image, zoom, checker, grid, v.integer(request.args.get('grid_size', 8), '网格大小', 1, 256))
            return image_response(image)

    @api.get('/projects/<key>/frames/<frame_id>/layers/<layer_id>/image')
    def layer_image(key, frame_id, layer_id):
        with store.lock:
            project = store.get(key)
            _, layer = find_layer(project, frame_id, layer_id)
            from .role_tools import display_image
            return image_response(display_image(store, project, layer))

    @api.post('/projects/<key>/frames')
    def add_frames(key):
        data = body()
        return jsonify(timeline.add(key, v.revision(data), data.get('after'), data.get('source'), data.get('count', 1)))

    @api.post('/projects/<key>/frames/remove')
    def remove_frames(key):
        data = body()
        return jsonify(timeline.remove(key, v.revision(data), data.get('frame_ids')))

    @api.post('/projects/<key>/frames/reorder')
    def reorder_frames(key):
        data = body()
        return jsonify(timeline.reorder(key, v.revision(data), data.get('frame_ids')))

    @api.patch('/projects/<key>/frames/<frame_id>')
    def update_frame(key, frame_id):
        data = body()
        return jsonify(timeline.update(key, v.revision(data), frame_id, data))

    @api.post('/projects/<key>/retime')
    def retime(key):
        data = body()
        return jsonify(timeline.retime(key, v.revision(data), selected(data, store.get(key)), data))

    @api.post('/projects/<key>/reverse')
    def reverse(key):
        data = body()
        return jsonify(timeline.reverse(key, v.revision(data), selected(data, store.get(key))))

    @api.post('/projects/<key>/pingpong')
    def pingpong(key):
        data = body()
        return jsonify(timeline.pingpong(key, v.revision(data), selected(data, store.get(key))))

    @api.get('/projects/<key>/schedule')
    def schedule(key):
        return jsonify(timeline.schedule(key, request.args.get('clip_id')))

    @api.post('/projects/<key>/clips')
    def clip(key):
        data = body()
        return jsonify(timeline.clip(key, v.revision(data), data))

    @api.delete('/projects/<key>/clips/<clip_id>')
    def remove_clip(key, clip_id):
        data = body()
        return jsonify(timeline.remove_clip(key, v.revision(data), clip_id))

    @api.post('/projects/<key>/frames/<frame_id>/layers')
    def add_layer(key, frame_id):
        data = body()
        return jsonify(layers.add(key, v.revision(data), frame_id, data.get('name', '新图层'), data.get('color', '#00000000')))

    @api.patch('/projects/<key>/frames/<frame_id>/layers/<layer_id>')
    def update_layer(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.update(key, v.revision(data), frame_id, layer_id, data))

    @api.delete('/projects/<key>/frames/<frame_id>/layers/<layer_id>')
    def remove_layer(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.remove(key, v.revision(data), frame_id, layer_id))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/duplicate')
    def duplicate_layer(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.duplicate(key, v.revision(data), frame_id, layer_id))

    @api.post('/projects/<key>/frames/<frame_id>/layers/reorder')
    def reorder_layers(key, frame_id):
        data = body()
        return jsonify(layers.reorder(key, v.revision(data), frame_id, data.get('layer_ids')))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/bake')
    def bake_layer(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.bake(key, v.revision(data), frame_id, layer_id))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/merge')
    def merge_layer(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.merge_down(key, v.revision(data), frame_id, layer_id))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/ungroup')
    def ungroup_layers(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.ungroup(key, v.revision(data), frame_id, layer_id))

    @api.post('/projects/<key>/flatten')
    def flatten(key):
        data = body()
        return jsonify(layers.flatten(key, v.revision(data), selected(data, store.get(key))))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/draw')
    def draw_layer(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.draw(key, v.revision(data), frame_id, layer_id, data))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/clear')
    def clear_layer(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.clear(key, v.revision(data), frame_id, layer_id))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/selection')
    def selection(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.selection(key, v.revision(data), frame_id, layer_id, data))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/propagate')
    def propagate(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.propagate(key, v.revision(data), frame_id, layer_id, data.get('frame_ids'), data.get('mode', 'append')))

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/stamp')
    def stamp(key, frame_id, layer_id):
        data = body()
        return jsonify(layers.stamp(key, v.revision(data), frame_id, layer_id, data))

    @api.post('/projects/<key>/effect')
    def effect(key):
        data = body()
        return jsonify(layers.effect(key, v.revision(data), data.get('frame_ids'), data.get('layer_id'), data))

    @api.post('/projects/<key>/pipeline')
    def pipeline(key):
        data = body()
        return jsonify(effect_pipeline(store, key, v.revision(data), data))

    @api.post('/projects/<key>/motion-trail')
    def trail(key):
        data = body()
        return jsonify(motion_trail(store, key, v.revision(data), data))

    @api.post('/projects/<key>/typewriter')
    def text_animation(key):
        data = body()
        return jsonify(typewriter(store, key, v.revision(data), data))

    @api.post('/projects/<key>/burst')
    def burst(key):
        data = body()
        return jsonify(radial_burst(store, key, v.revision(data), data))

    @api.post('/projects/<key>/temporal-average')
    def average(key):
        data = body()
        return jsonify(temporal_average(store, key, v.revision(data), data))

    @api.post('/projects/<key>/tween/transform')
    def tween_transform(key):
        data = body()
        return jsonify(tween.transform(key, v.revision(data), data))

    @api.post('/projects/<key>/tween/crossfade')
    def tween_fade(key):
        data = body()
        return jsonify(tween.crossfade(key, v.revision(data), data))

    @api.post('/projects/<key>/tween/path')
    def tween_path(key):
        data = body()
        return jsonify(tween.path(key, v.revision(data), data))

    @api.post('/projects/<key>/tween/loop')
    def tween_loop(key):
        data = body()
        return jsonify(tween.procedural_loop(key, v.revision(data), data))

    @api.post('/projects/<key>/resize')
    def resize(key):
        data = body()
        return jsonify(resize_canvas(store, key, v.revision(data), data))

    @api.post('/projects/<key>/crop')
    def crop(key):
        data = body()
        return jsonify(crop_all(store, key, v.revision(data), data))

    @api.post('/projects/<key>/trim')
    def trim(key):
        data = body()
        return jsonify(trim_all(store, key, v.revision(data), data.get('padding', 0)))

    @api.post('/projects/<key>/align')
    def align(key):
        data = body()
        return jsonify(align_content(store, key, v.revision(data), selected(data, store.get(key)), data.get('anchor', 'center')))

    @api.post('/projects/<key>/rotate')
    def rotate(key):
        data = body()
        return jsonify(rotate_canvas(store, key, v.revision(data), data.get('turns', 1)))

    @api.post('/projects/<key>/ninepatch')
    def ninepatch(key):
        return jsonify(ninepatch_project(store, key, body())), 201

    @api.post('/projects/<key>/seamless')
    def seamless(key):
        data = body()
        return jsonify(seamless_frames(store, key, v.revision(data), data))

    @api.post('/projects/<key>/import')
    def import_frames(key):
        files = request.files.getlist('files')
        revision = v.integer(request.form.get('revision'), '修订号', 0)
        return jsonify(import_into_project(store, key, revision, [(file.filename, file.stream) for file in files], request.form.get('mode', 'append'), request.form.get('fit', 'center'), request.form.get('frame_id')))

    @api.post('/projects/<key>/slice')
    def slice_image(key):
        data = json.loads(request.form.get('parameters', '{}'))
        v.object_value(data)
        return jsonify(slice_sheet(store, key, v.revision(data), uploaded().stream, data))

    @api.post('/projects/<key>/palette/extract')
    def extract_palette(key):
        data = body()
        return jsonify(palettes.extract(key, selected(data, store.get(key)), data.get('count', 16), data.get('method', 'median')))

    @api.post('/projects/<key>/palette/apply')
    def apply_palette(key):
        data = body()
        return jsonify(palettes.apply(key, v.revision(data), selected(data, store.get(key)), data.get('palette'), data.get('dither', 'none')))

    @api.post('/projects/<key>/palette/cycle')
    def cycle_palette(key):
        data = body()
        return jsonify(palettes.cycle(key, v.revision(data), data.get('frame_id'), data.get('count', 6)))

    @api.post('/projects/<key>/ink-roles')
    def ink_roles(key):
        from . import role_tools
        data = body()
        return jsonify(role_tools.update_roles(store, key, v.revision(data), data.get('roles')))

    @api.get('/projects/<key>/ink-roles/status')
    def ink_role_status(key):
        from . import role_tools
        with store.lock:
            project = store.get(key)
            return jsonify({'roles': role_tools.entries(project), 'layers': role_tools.summary(store, project)})

    @api.post('/projects/<key>/frames/<frame_id>/layers/<layer_id>/ink/<operation>')
    def ink_operation(key, frame_id, layer_id, operation):
        from . import role_tools
        data = body()
        if operation == 'enable':
            result = role_tools.enable(store, key, v.revision(data), frame_id, layer_id)
        elif operation == 'materialize':
            result = role_tools.materialize(store, key, v.revision(data), frame_id, layer_id)
        elif operation == 'assign':
            result = role_tools.assign_region(store, key, v.revision(data), frame_id, layer_id, data)
        else:
            raise v.Invalid('未知角色色操作')
        return jsonify(result)

    @api.post('/palette/ramp')
    def color_ramp():
        data = body()
        count = v.integer(data.get('count', 8), '色阶数量', 2, 64)
        space = v.choice(data.get('space', 'rgb'), '色彩空间', ['rgb', 'hsv'])
        return jsonify({'palette': ramp(data.get('first', '#152437ff'), data.get('second', '#65e2b9ff'), count, space)})

    @api.post('/projects/<key>/palette/inventory')
    def colors(key):
        data = body()
        with store.lock:
            project = store.get(key)
            ids = v.frame_ids(selected(data, project), project)
            return jsonify(color_inventory([render_frame(store, project, frame_id) for frame_id in ids]))

    @api.get('/projects/<key>/analysis')
    def analysis(key):
        with store.lock:
            return jsonify(animation_report(store, store.get(key)))

    @api.get('/projects/<key>/frames/<frame_id>/metrics')
    def frame_analysis(key, frame_id):
        with store.lock:
            return jsonify(frame_metrics(render_frame(store, store.get(key), frame_id)))

    @api.get('/projects/<key>/frames/<frame_id>/seam')
    def seam(key, frame_id):
        with store.lock:
            return jsonify(seam_metrics(render_frame(store, store.get(key), frame_id)))

    @api.get('/projects/<key>/loop-seam')
    def loop_difference(key):
        with store.lock:
            image, report = loop_seam(store, store.get(key))
            if request.args.get('image') == 'true':
                return image_response(image)
            return jsonify(report)

    @api.post('/projects/<key>/difference')
    def difference_frames(key):
        data = body()
        with store.lock:
            project = store.get(key)
            first = render_frame(store, project, data.get('first'))
            second = render_frame(store, project, data.get('second'))
            image, details = difference(first, second)
            if data.get('image') is True:
                return image_response(image)
            return jsonify(details)

    @api.post('/projects/<key>/collision')
    def collision(key):
        data = body()
        with store.lock:
            project = store.get(key)
            first = render_frame(store, project, data.get('first'))
            second = render_frame(store, project, data.get('second'))
            dx = v.integer(data.get('dx', 0), '碰撞偏移X', -2048, 2048)
            dy = v.integer(data.get('dy', 0), '碰撞偏移Y', -2048, 2048)
            threshold = v.integer(data.get('threshold', 1), '透明度阈值', 1, 255)
            image, details = alpha_collision(first, second, dx, dy, threshold)
            return image_response(image) if data.get('image') is True else jsonify(details)

    @api.post('/projects/<key>/deduplicate')
    def deduplicate(key):
        data = body()
        adjacent = v.boolean(data.get('adjacent_only', True), '只合并相邻')
        return jsonify(duplicate_cleanup(store, key, v.revision(data), adjacent))

    @api.get('/projects/<key>/contact-sheet')
    def contact(key):
        with store.lock:
            return image_response(motion_contact_sheet(store, store.get(key)))

    @api.get('/projects/<key>/frames/<frame_id>/repeat')
    def repeat(key, frame_id):
        with store.lock:
            image = render_frame(store, store.get(key), frame_id)
            return image_response(repeat_preview(image))

    @api.post('/projects/<key>/atlas-preview')
    def atlas_preview(key):
        with store.lock:
            image, metadata = build_atlas(store, store.get(key), body())
            return jsonify(metadata) if request.args.get('metadata') == 'true' else image_response(image)

    @api.post('/projects/<key>/export-preflight')
    def preflight(key):
        return jsonify(export_preflight(store.get(key), body()))

    @api.get('/projects/<key>/report')
    def report(key):
        with store.lock:
            return Response(project_report(store, store.get(key)), mimetype='text/html')

    @api.get('/projects/<key>/timeline.csv')
    def export_csv(key):
        return download(timeline_csv(store.get(key)), 'timeline.csv', 'text/csv')

    @api.get('/projects/<key>/jobs')
    def project_jobs(key):
        store.get(key)
        return jsonify(jobs.list(key))

    @api.post('/projects/<key>/jobs')
    def start_job(key):
        return jsonify(jobs.start(key, body())), 202

    @api.get('/jobs/<job_id>')
    def job(job_id):
        return jsonify(jobs.get(job_id))

    @api.post('/jobs/<job_id>/cancel')
    def cancel_job(job_id):
        return jsonify(jobs.cancel(job_id))

    @api.post('/jobs/<job_id>/retry')
    def retry_job(job_id):
        return jsonify(jobs.retry(job_id)), 202

    @api.get('/jobs/<job_id>/download')
    def download_job(job_id):
        path, job = jobs.download(job_id)
        return send_file(path, mimetype=job['mimetype'], as_attachment=True, download_name=job['filename'])

    @api.get('/recipes')
    def list_recipes():
        return jsonify(recipes.list())

    @api.post('/recipes')
    def create_recipe():
        return jsonify(recipes.create(body())), 201

    @api.put('/recipes/<recipe_id>')
    def update_recipe(recipe_id):
        return jsonify(recipes.update(recipe_id, body()))

    @api.post('/recipes/import')
    def import_recipe():
        return jsonify(recipes.import_recipe(body())), 201

    @api.get('/recipes/<recipe_id>/export')
    def export_recipe(recipe_id):
        return download(json_bytes(recipes.export(recipe_id)), 'pixel-recipe.json', 'application/json')

    @api.post('/projects/<key>/recipes/<recipe_id>/apply')
    def apply_recipe(key, recipe_id):
        data = body()
        return jsonify(recipes.apply(key, v.revision(data), recipe_id, selected(data, store.get(key))))

    @api.post('/projects/<key>/maps')
    def create_map(key):
        data = body()
        return jsonify(tilemaps.create(key, v.revision(data), data))

    @api.post('/projects/<key>/maps/<map_id>/paint')
    def paint_map(key, map_id):
        data = body()
        return jsonify(tilemaps.paint(key, v.revision(data), map_id, data))

    @api.post('/projects/<key>/maps/<map_id>/fill')
    def fill_map(key, map_id):
        data = body()
        return jsonify(tilemaps.fill(key, v.revision(data), map_id, data))

    @api.post('/projects/<key>/maps/<map_id>/layers')
    def add_map_layer(key, map_id):
        data = body()
        return jsonify(tilemaps.layer(key, v.revision(data), map_id, data))

    @api.delete('/projects/<key>/maps/<map_id>')
    def remove_map(key, map_id):
        data = body()
        with store.edit(key, v.revision(data), '移除瓦片地图') as project:
            from .tilemaps import find_map
            project['tilemaps'].remove(find_map(project, map_id))
        return jsonify(store.get(key))

    @api.get('/projects/<key>/maps/<map_id>/image')
    def map_image(key, map_id):
        with store.lock:
            elapsed = v.integer(request.args.get('time_ms', 0), '地图预览时间', 0, 1000000000000)
            if request.args.get('export_preview') == 'true':
                return image_response(tilemaps.render_export(key, map_id, elapsed))
            return image_response(tilemaps.render(key, map_id, elapsed))

    @api.get('/projects/<key>/maps/<map_id>/export')
    def map_export(key, map_id):
        with store.lock:
            return download(tiled_bundle(store, tilemaps, store.get(key), map_id), 'tilemap.zip', 'application/zip')

    @api.post('/projects/<key>/matte')
    def matte(key):
        from .matting import matte_layers
        data = body()
        return jsonify(matte_layers(store, key, v.revision(data), data))

    @api.get('/projects/<key>/frames/<frame_id>/alpha')
    def alpha_analysis(key, frame_id):
        from .matting import alpha_report
        with store.lock:
            return jsonify(alpha_report(render_frame(store, store.get(key), frame_id)))

    @api.post('/projects/<key>/collision-boxes')
    def collision_boxes(key):
        from .collision import auto_collision_boxes
        data = body()
        return jsonify(auto_collision_boxes(store, key, v.revision(data), data))

    @api.get('/projects/<key>/frames/<frame_id>/collision-preview')
    def collision_image(key, frame_id):
        from .collision import collision_preview
        with store.lock:
            return image_response(collision_preview(store, store.get(key), frame_id))

    @api.get('/projects/<key>/collision-metadata')
    def collision_metadata(key):
        from .collision import metadata_sequence
        return jsonify(metadata_sequence(store.get(key)))

    @api.get('/projects/<key>/snapshots')
    def snapshot_list(key):
        from .snapshots import Snapshots
        return jsonify(Snapshots(store).list(key))

    @api.post('/projects/<key>/snapshots')
    def snapshot_create(key):
        from .snapshots import Snapshots
        return jsonify(Snapshots(store).create(key, body())), 201

    @api.post('/projects/<key>/snapshots/<snapshot_id>/restore')
    def snapshot_restore(key, snapshot_id):
        from .snapshots import Snapshots
        data = body()
        return jsonify(Snapshots(store).restore(key, v.revision(data), snapshot_id))

    @api.post('/projects/<key>/snapshots/<snapshot_id>/fork')
    def snapshot_fork(key, snapshot_id):
        from .snapshots import Snapshots
        return jsonify(Snapshots(store).fork(key, snapshot_id)), 201

    @api.post('/projects/<key>/snapshots/compare')
    def snapshot_compare(key):
        from .snapshots import Snapshots
        data = body()
        return jsonify(Snapshots(store).compare(key, data.get('first'), data.get('second')))

    @api.post('/projects/<key>/resample')
    def resample(key):
        from .resampling import resample_project
        return jsonify(resample_project(store, key, body())), 201

    @api.post('/projects/<key>/sampling-plan')
    def sampling_plan(key):
        from .resampling import temporal_plan
        return jsonify(temporal_plan(store.get(key), body()))

    @api.get('/projects/<key>/exposure-chart')
    def exposure(key):
        from .resampling import exposure_chart
        return image_response(exposure_chart(store.get(key)))

    @api.post('/auto-slice')
    def automatic_sprite_slice():
        from .autoslice import create_from_sprites
        file = uploaded()
        try:
            parameters = json.loads(request.form.get('parameters', '{}'))
        except json.JSONDecodeError as error:
            raise v.Invalid('切片参数JSON无效') from error
        return jsonify(create_from_sprites(store, file.stream, file.filename, v.object_value(parameters))), 201

    @api.post('/projects/<key>/frames/<frame_id>/sample')
    def sample_color(key, frame_id):
        from .sampling import sample_pixel, nearest_saved_color
        data = body()
        with store.lock:
            project = store.get(key)
            image = render_frame(store, project, frame_id)
            sampled = sample_pixel(image, data)
            sampled['palette_match'] = nearest_saved_color(sampled['hex'], project['palette'])
            return jsonify(sampled)

    @api.post('/projects/<key>/frames/<frame_id>/region-color')
    def region_color(key, frame_id):
        from .sampling import sample_region
        with store.lock:
            image = render_frame(store, store.get(key), frame_id)
            return jsonify(sample_region(image, body()))

    @api.post('/projects/<key>/maps/<map_id>/animation')
    def bind_map_animation(key, map_id):
        from .map_animation import bind_animation
        data = body()
        return jsonify(bind_animation(store, key, v.revision(data), map_id, data.get('tile'), data.get('clip_id')))

    @api.post('/compositions')
    def compose_shot():
        from .composition import compose
        return jsonify(compose(store, body())), 201

    @api.post('/projects/<key>/cels/link')
    def link_cels(key):
        from .cels import link
        data = body()
        return jsonify(link(store, key, data['revision'], data['source_frame'], data['source_layer'], data['targets']))

    @api.post('/projects/<key>/frames/<frame>/layers/<layer>/cel-detach')
    def detach_cel(key, frame, layer):
        from .cels import detach
        data = body()
        return jsonify(detach(store, key, data['revision'], frame, layer))

    @api.post('/projects/<key>/atlas-workspaces')
    def atlas_workspace_create(key):
        from .atlas_workspaces import build
        data = body()
        return jsonify(build(store, key, v.revision(data), data)), 201

    @api.post('/projects/<key>/atlas-workspaces/<identifier>/build')
    def atlas_workspace_build(key, identifier):
        from .atlas_workspaces import build
        data = body()
        return jsonify(build(store, key, v.revision(data), data, identifier))

    @api.get('/projects/<key>/atlas-workspaces/<identifier>/versions/<int:number>/preview')
    def atlas_workspace_preview(key, identifier, number):
        from .atlas_workspaces import find
        project = store.get(key)
        workspace = find(project, identifier)
        version = next((item for item in workspace['versions'] if item['number'] == number), None)
        if version is None:
            raise v.Invalid('图集版本不存在')
        return image_response(store.image(key, version['page_asset']))

    @api.get('/projects/<key>/atlas-workspaces/<identifier>/versions/<int:number>/download')
    def atlas_workspace_download(key, identifier, number):
        from .atlas_workspaces import bundle
        payload = bundle(store, key, identifier, number)
        return send_file(io.BytesIO(payload), mimetype='application/zip', as_attachment=True,
                         download_name=f'atlas-version-{number}.zip')

    @api.get('/projects/<key>/atlas-workspaces/<identifier>/compare')
    def atlas_workspace_compare(key, identifier):
        from .atlas_workspaces import compare
        first = v.integer(request.args.get('first'), '对比起始版本', 1, 10000)
        second = v.integer(request.args.get('second'), '对比结束版本', 1, 10000)
        with store.lock:
            image, report = compare(store, key, identifier, first, second)
        if request.args.get('image') == 'true':
            return image_response(image)
        return jsonify(report)

    @api.get('/projects/<key>/atlas-workspaces/<identifier>/versions/<int:number>/entries')
    def atlas_workspace_entries(key, identifier, number):
        from .atlas_workspaces import find
        project = store.get(key)
        workspace = find(project, identifier)
        version = next((item for item in workspace['versions'] if item['number'] == number), None)
        if version is None:
            raise v.Invalid('图集版本不存在')
        return jsonify({'number': version['number'], 'source_revision': version['source_revision'],
                        'page_sha256': version.get('page_sha256'), 'entries': version['entries']})

    return api
