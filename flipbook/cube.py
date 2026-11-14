"""Cube texture painting workspaces: map six square frames to cube faces."""
import io
import json
import zipfile
from PIL import Image, ImageDraw
from . import validate as v
from .model import new_id, find_frame
from .composite import render_frame, png_bytes
from .drawing import apply_paint, erase_pixels
from .layer_nodes import walk_layers

FACE_KEYS = ['px', 'nx', 'py', 'ny', 'pz', 'nz']
FACE_LABELS = {'px': '右面 +X', 'nx': '左面 −X', 'py': '顶面 +Y', 'ny': '底面 −Y', 'pz': '前面 +Z', 'nz': '后面 −Z'}
NET_LAYOUT = {'py': (1, 0), 'nx': (0, 1), 'pz': (1, 1), 'px': (2, 1), 'nz': (3, 1), 'ny': (1, 2)}
MAX_CUBES = 20
MAX_RUNS_PER_FACE = 4096


def workspaces(project):
    return project.get('cubes', [])


def find_cube(project, cube_id):
    v.key(cube_id)
    result = next((item for item in workspaces(project) if item['id'] == cube_id), None)
    if result is None:
        raise v.Invalid('立方体工作区不存在')
    return result


def require_square(project):
    if project['width'] != project['height']:
        raise v.Invalid('立方体纹理需要正方形画布，请先把画布调整为正方形')


def face_mapping(project, body):
    faces = v.object_value(body.get('faces'), '立方体面映射')
    if set(faces) != set(FACE_KEYS):
        raise v.Invalid('立方体需要六个面的完整映射')
    frames = {frame['id']: frame for frame in project['frames']}
    used = set()
    mapping = {}
    for key in FACE_KEYS:
        entry = v.object_value(faces[key], '立方体面映射')
        frame_id = v.key(entry.get('frame'))
        frame = frames.get(frame_id)
        if frame is None:
            raise v.Invalid('立方体引用了不存在的帧')
        if frame_id in used:
            raise v.Invalid('六个面需要六张不同的帧')
        used.add(frame_id)
        layer_id = entry.get('layer')
        if layer_id is not None:
            v.key(layer_id)
            if not any(layer['id'] == layer_id for layer in walk_layers(frame['layers'])):
                raise v.Invalid('立方体引用了不存在的图层')
        else:
            layer_id = frame['layers'][-1]['id']
        mapping[key] = {'frame': frame_id, 'layer': layer_id}
    return mapping


def create(store, project_id, revision, body):
    with store.edit(project_id, revision, '创建立方体工作区') as project:
        require_square(project)
        collection = project.setdefault('cubes', [])
        if len(collection) >= MAX_CUBES:
            raise v.Invalid(f'每项目最多{MAX_CUBES}个立方体工作区')
        cube = {
            'id': new_id(),
            'name': v.text(body.get('name', '立方体纹理'), '立方体名称', 100, False),
            'faces': face_mapping(project, body),
        }
        collection.append(cube)
    return store.get(project_id)


def update(store, project_id, revision, cube_id, body):
    with store.edit(project_id, revision, '立方体面映射') as project:
        cube = find_cube(project, cube_id)
        require_square(project)
        if 'name' in body:
            cube['name'] = v.text(body['name'], '立方体名称', 100, False)
        if 'faces' in body:
            cube['faces'] = face_mapping(project, body)
    return store.get(project_id)


def remove(store, project_id, revision, cube_id):
    with store.edit(project_id, revision, '移除立方体工作区') as project:
        cube = find_cube(project, cube_id)
        project['cubes'].remove(cube)
    return store.get(project_id)


def paint_layer(project, cube, face_key):
    face = cube['faces'][face_key]
    frame = find_frame(project, face['frame'])
    layers = list(walk_layers(frame['layers']))
    layer = next((item for item in layers if item['id'] == face['layer']), None)
    if layer is None:
        layer = frame['layers'][-1]
        face['layer'] = layer['id']
    if layer.get('kind') == 'group':
        raise v.Invalid(f'{FACE_LABELS[face_key]}的目标图层是组合，请展开组合或在映射中更换图层')
    if layer['locked']:
        raise v.Invalid(f'{FACE_LABELS[face_key]}的目标图层已锁定，请先解锁')
    if layer.get('cel_id'):
        raise v.Invalid(f'{FACE_LABELS[face_key]}的目标图层是共用原画实例，请先脱离')
    from .role_tools import require_ordinary
    require_ordinary(layer)
    if layer.get('affine') or layer['transform'] != v.transform({}):
        raise v.Invalid(f'{FACE_LABELS[face_key]}的目标图层存在变换，请先在主画布烘焙变换')
    return frame, layer


def runs_mask(size, runs):
    mask = Image.new('L', (size, size), 0)
    painter = ImageDraw.Draw(mask)
    covered = 0
    for run in v.array(runs, '绘制行段', MAX_RUNS_PER_FACE):
        values = v.array(run, '绘制行段', 3)
        if len(values) != 3:
            raise v.Invalid('绘制行段需要[y, x0, x1]三个整数')
        y = v.integer(values[0], '行号', 0, size - 1)
        x0 = v.integer(values[1], '行段起点', 0, size - 1)
        x1 = v.integer(values[2], '行段终点', 0, size - 1)
        if x1 < x0:
            x0, x1 = x1, x0
        painter.line((x0, y, x1, y), fill=255)
        covered += x1 - x0 + 1
    return mask, covered


def paint(store, project_id, revision, cube_id, body):
    with store.edit(project_id, revision, '立方体表面绘制') as project:
        require_square(project)
        size = project['width']
        cube = find_cube(project, cube_id)
        faces_body = v.object_value(body.get('faces'), '立方体绘制数据')
        if not faces_body:
            raise v.Invalid('这一笔没有覆盖任何面')
        touched = 0
        pending = []
        for face_key, spec in faces_body.items():
            if face_key not in FACE_KEYS:
                raise v.Invalid('未知的立方体面：' + str(face_key))
            spec = v.object_value(spec, '单面绘制数据')
            tool = v.choice(spec.get('tool', 'pencil'), '绘制工具', ['pencil', 'erase'])
            color = v.rgba(spec.get('color', '#65e2b9ff'))
            mask, covered = runs_mask(size, spec.get('runs'))
            if not covered:
                continue
            frame, layer = paint_layer(project, cube, face_key)
            pending.append((layer, mask, tool, color))
            touched += covered
        if not touched:
            raise v.Invalid('这一笔没有覆盖任何像素')
        for layer, mask, tool, color in pending:
            image = store.image(project_id, layer['asset'])
            if tool == 'erase':
                image = erase_pixels(image, mask)
            else:
                image = apply_paint(image, mask, color)
            layer['asset'] = store.put_image(project_id, image)
    return store.get(project_id)


def net_image(store, project, cube):
    size = project['width']
    gap = max(1, size // 64)
    sheet = Image.new('RGBA', (4 * size + 5 * gap, 3 * size + 4 * gap))
    for key, (column, row) in NET_LAYOUT.items():
        image = render_frame(store, project, cube['faces'][key]['frame'])
        sheet.alpha_composite(image, (gap + column * (size + gap), gap + row * (size + gap)))
    return sheet


def bundle(store, project, cube_id):
    cube = find_cube(project, cube_id)
    metadata = {
        'format': 'flipbook-cube-textures-v1',
        'name': cube['name'],
        'size': project['width'],
        'note': '六个面为对应帧的合成图像；net.png为十字展开布局',
        'faces': {},
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for key in FACE_KEYS:
            face = cube['faces'][key]
            image = render_frame(store, project, face['frame'])
            archive.writestr(f'faces/{key}.png', png_bytes(image))
            metadata['faces'][key] = {'file': f'faces/{key}.png', 'label': FACE_LABELS[key], 'frame_id': face['frame']}
        archive.writestr('net.png', png_bytes(net_image(store, project, cube)))
        archive.writestr('cube.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    return stream.getvalue()


def validate_cubes(project):
    frame_ids = {frame['id'] for frame in project['frames']}
    cube_ids = set()
    for cube in v.array(project.get('cubes', []), '立方体工作区', MAX_CUBES):
        cube_id = v.key(cube.get('id'))
        if cube_id in cube_ids:
            raise v.Invalid('立方体工作区编号重复')
        cube_ids.add(cube_id)
        v.text(cube.get('name'), '立方体名称', 100, False)
        faces = v.object_value(cube.get('faces'), '立方体面映射')
        if set(faces) != set(FACE_KEYS):
            raise v.Invalid('立方体面映射不完整')
        used = set()
        for key in FACE_KEYS:
            entry = v.object_value(faces[key], '立方体面映射')
            frame_id = v.key(entry.get('frame'))
            if frame_id not in frame_ids:
                raise v.Invalid('立方体工作区引用的帧不存在，请先调整面映射或移除工作区')
            if frame_id in used:
                raise v.Invalid('立方体六个面需要六张不同的帧')
            used.add(frame_id)
            v.key(entry.get('layer'))
