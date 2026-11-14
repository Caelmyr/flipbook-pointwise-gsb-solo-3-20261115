"""Compile a shot from captured source states into independent editable cels."""
from copy import deepcopy
from PIL import Image
from . import validate as v
from .model import find_frame, new_frame, new_layer


def source_sequence(project, clip_id=None):
    frames = [(frame['id'], frame['duration']) for frame in project['frames']]
    if clip_id:
        clip = next((item for item in project['clips'] if item['id'] == clip_id), None)
        if clip is None:
            raise v.Invalid('镜头轨道引用的动作片段不存在')
        frames = [(key, find_frame(project, key)['duration']) for key in clip['frame_ids']]
        if clip.get('exposures'):
            frames = [(item['frame_id'], item['duration']) for item in clip['exposures']]
        if clip['direction'] == 'reverse':
            frames.reverse()
        elif clip['direction'] == 'pingpong':
            frames += frames[-2:0:-1]
    return frames


def prepare(store, body):
    total = v.integer(body.get('total', 1000), '镜头总时长', 20, 30000)
    name = v.text(body.get('name', '分轨镜头'), '镜头名称', 100, False)
    tracks = []
    captured = {}
    for item in v.array(body.get('tracks'), '镜头轨道', 4):
        key = v.key(item.get('project_id'))
        if key not in captured:
            captured[key] = store.get(key)
        project = captured[key]
        sequence = source_sequence(project, item.get('clip_id'))
        tracks.append({'project': project, 'sequence': sequence,
                       'clip_id': item.get('clip_id'),
                       'name': v.text(item.get('name', project['name']), '轨道名称', 100, False),
                       'start': v.integer(item.get('start', 0), '轨道开始时间', 0, total),
                       'repeat': v.integer(item.get('repeat', 1), '轨道播放轮数', 0, 1000),
                       'hold': v.boolean(item.get('hold', False), '结束停留'),
                       'x': v.integer(item.get('x', 0), '轨道位置X', -2048, 2048),
                       'y': v.integer(item.get('y', 0), '轨道位置Y', -2048, 2048)})
    if not tracks:
        raise v.Invalid('至少选择一条镜头轨道')
    sizes = {(track['project']['width'], track['project']['height']) for track in tracks}
    if len(sizes) != 1:
        raise v.Invalid('当前镜头使用同尺寸项目，请先统一源动画画布')
    return name, total, tracks, sizes.pop()


def frame_at(track, elapsed):
    if elapsed < track['start']:
        return None
    sequence = track['sequence']
    cycle = sum(duration for _, duration in sequence)
    relative = elapsed - track['start']
    if track['repeat'] and relative >= track['repeat'] * cycle:
        return sequence[-1][0] if track['hold'] else None
    position = relative % cycle
    for frame_id, duration in sequence:
        if position < duration:
            return frame_id
        position -= duration
    raise v.Invalid('轨道曝光序列无效')


def compose(store, body):
    with store.lock:
        name, total, tracks, size = prepare(store, body)
        fps = v.integer(body.get('fps', 25), '镜头采样帧率', 1, 50)
        duration = max(20, round(1000 / fps))
        points = list(range(0, total, duration))
        if len(points) > 300:
            raise v.Invalid('镜头采样超过300帧')
        v.pixel_budget(*size, len(points) * len(tracks))
        created = store.create(name, *size)
        key = created['id']
        blank = store.put_image(key, Image.new('RGBA', size))
        with store.edit(key, 0, '生成分轨镜头') as project:
            project['frames'] = []
            project['loop'] = 1
            for at in points:
                frame = new_frame(blank, f'镜头 {at}ms', max(20, min(duration, total-at)))
                frame['layers'] = []
                for track in tracks:
                    layer = new_layer(track['name'], blank, transform=v.transform({'x':track['x'],'y':track['y']}))
                    layer['scene_source'] = {'project_id':track['project']['id'],'clip_id':track['clip_id'],
                                             'start':track['start'],'repeat':track['repeat'],'hold':track['hold'],
                                             'at':at}
                    frame['layers'].append(layer)
                project['frames'].append(frame)
            project['composition'] = {'requested_duration':total,'sources':[t['project']['id'] for t in tracks]}
        return store.get(key)


def render_external(store, reference):
    from .composite import render_frame
    project = store.get(reference['project_id'])
    track = {'start':reference['start'],'repeat':reference['repeat'],'hold':reference['hold'],
             'sequence':source_sequence(project, reference['clip_id'])}
    frame = frame_at(track,reference['at'])
    if frame is None:
        return Image.new('RGBA',(project['width'],project['height']))
    return render_frame(store,project,frame)
