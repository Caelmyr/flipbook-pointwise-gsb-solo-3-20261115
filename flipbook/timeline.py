from copy import deepcopy
from PIL import Image
from . import validate as v
from .model import find_frame, new_frame, clone_frame, new_id, remove_camera_for_frames


class Timeline:
    def __init__(self, store):
        self.store = store

    def add(self, project_id, revision, after=None, source=None, count=1):
        count = v.integer(count, '新增帧数', 1, 60)
        with self.store.edit(project_id, revision, '添加动画帧') as project:
            if len(project['frames']) + count > 300:
                raise v.Invalid('动画最多300帧')
            index = len(project['frames']) if after is None else project['frames'].index(find_frame(project, after)) + 1
            blank = self.store.put_image(project_id, Image.new('RGBA', (project['width'], project['height'])))
            frames = []
            for i in range(count):
                if source:
                    frame = clone_frame(find_frame(project, source))
                else:
                    frame = new_frame(blank, f'帧 {index + i + 1}')
                frames.append(frame)
            project['frames'][index:index] = frames
        return self.store.get(project_id)

    def remove(self, project_id, revision, ids):
        with self.store.edit(project_id, revision, '移除动画帧') as project:
            ids = v.frame_ids(ids, project)
            if len(ids) == len(project['frames']):
                raise v.Invalid('动画至少保留一帧')
            project['frames'] = [f for f in project['frames'] if f['id'] not in ids]
            for clip in project['clips']:
                clip['frame_ids'] = [key for key in clip['frame_ids'] if key not in ids]
                if 'exposures' in clip:
                    clip['exposures'] = [item for item in clip['exposures'] if item['frame_id'] not in ids]
            project['clips'] = [clip for clip in project['clips'] if clip['frame_ids']]
            remove_camera_for_frames(project, ids)
        return self.store.get(project_id)

    def reorder(self, project_id, revision, ids):
        with self.store.edit(project_id, revision, '时间轴排序') as project:
            ids = v.frame_ids(ids, project)
            if len(ids) != len(project['frames']):
                raise v.Invalid('排序必须包含全部帧')
            lookup = {frame['id']: frame for frame in project['frames']}
            project['frames'] = [lookup[key] for key in ids]
        return self.store.get(project_id)

    def update(self, project_id, revision, frame_id, body):
        with self.store.edit(project_id, revision, '帧属性') as project:
            frame = find_frame(project, frame_id)
            if 'name' in body:
                frame['name'] = v.text(body['name'], '帧名称', 100, False)
            if 'notes' in body:
                frame['notes'] = v.text(body['notes'], '帧说明', 2000)
            if 'duration' in body:
                frame['duration'] = v.frame_duration(body['duration'], project)
            if 'pivot' in body:
                pivot = v.array(body['pivot'], '枢轴', 2)
                if len(pivot) != 2:
                    raise v.Invalid('枢轴需要两个分量')
                frame['pivot'] = [v.real(item, '枢轴分量', 0, 1) for item in pivot]
            if 'boxes' in body:
                boxes = []
                for box in v.array(body['boxes'], '碰撞框', 30):
                    validated = v.rect(box, project['width'], project['height'])
                    validated['name'] = v.text(box.get('name', '碰撞框'), '碰撞框名称', 60)
                    boxes.append(validated)
                frame['boxes'] = boxes
        return self.store.get(project_id)

    def retime(self, project_id, revision, ids, body):
        mode = v.choice(body.get('mode', 'duration'), '重定时模式', ['duration', 'fps', 'scale', 'total'])
        with self.store.edit(project_id, revision, '动画重定时') as project:
            ids = v.frame_ids(ids, project)
            frames = [find_frame(project, key) for key in ids]
            original_total = sum(frame['duration'] for frame in frames)
            minimum = 1 if project.get('animation_timing') == 'exact_composition' else 20
            if mode == 'duration':
                durations = [v.frame_duration(body.get('duration', 100), project)] * len(frames)
            elif mode == 'fps':
                fps = v.real(body.get('fps', 12), '帧率', .1, 50)
                durations = [v.frame_duration(round(1000 / fps), project)] * len(frames)
            else:
                if mode == 'scale':
                    factor = v.real(body.get('factor', 1), '时长倍数', .05, 20)
                    target = int(round(original_total * factor))
                else:
                    target = v.integer(body.get('total', 1000), '目标总时长', len(frames) * minimum, len(frames) * 10000)
                if not len(frames) * minimum <= target <= len(frames) * 10000:
                    raise v.Invalid(f'目标总时长无法满足每帧{minimum}–10000毫秒限制')
                ideal = [frame['duration'] / original_total * target for frame in frames]
                durations = [int(value) for value in ideal]
                remainder = target - sum(durations)
                order = sorted(range(len(frames)), key=lambda i: ideal[i] - durations[i], reverse=True)
                for index in order[:remainder]:
                    durations[index] += 1
                for duration in durations:
                    v.frame_duration(duration, project)
            for frame, duration in zip(frames, durations):
                frame['duration'] = duration
        return self.store.get(project_id)

    def reverse(self, project_id, revision, ids):
        with self.store.edit(project_id, revision, '反转选中帧') as project:
            ids = v.frame_ids(ids, project)
            indexes = [i for i, frame in enumerate(project['frames']) if frame['id'] in ids]
            reversed_frames = [project['frames'][i] for i in indexes][::-1]
            for index, frame in zip(indexes, reversed_frames):
                project['frames'][index] = frame
        return self.store.get(project_id)

    def pingpong(self, project_id, revision, ids):
        with self.store.edit(project_id, revision, '往返循环') as project:
            ids = v.frame_ids(ids, project, 3)
            source = [frame for frame in project['frames'] if frame['id'] in ids]
            if len(project['frames']) + len(source) - 2 > 300:
                raise v.Invalid('往返展开超过300帧')
            last_index = max(project['frames'].index(frame) for frame in source)
            copies = [clone_frame(frame) for frame in source[-2:0:-1]]
            project['frames'][last_index + 1:last_index + 1] = copies
        return self.store.get(project_id)

    def hold(self, project_id, revision, frame_id, count):
        return self.add(project_id, revision, after=frame_id, source=frame_id, count=count)

    def clip(self, project_id, revision, body):
        with self.store.edit(project_id, revision, '动画片段') as project:
            ids = v.frame_ids(body.get('frame_ids'), project)
            name = v.text(body.get('name'), '片段名称', 100, False)
            clip_id = body.get('id')
            if clip_id:
                v.key(clip_id)
                clip = next((c for c in project['clips'] if c['id'] == clip_id), None)
                if clip is None:
                    raise v.Invalid('片段不存在')
            else:
                if len(project['clips']) >= 100:
                    raise v.Invalid('最多100个动画片段')
                clip = {'id': new_id()}
                project['clips'].append(clip)
            clip.pop('exposures', None)
            clip.update({
                'name': name,
                'frame_ids': ids,
                'direction': v.choice(body.get('direction', 'forward'), '播放方向', ['forward', 'reverse', 'pingpong']),
                'repeat': v.integer(body.get('repeat', 0), '循环次数', 0, 65535),
            })
        return self.store.get(project_id)

    def remove_clip(self, project_id, revision, clip_id):
        with self.store.edit(project_id, revision, '移除动画片段') as project:
            clip = next((c for c in project['clips'] if c['id'] == clip_id), None)
            if clip is None:
                raise v.Invalid('片段不存在')
            project['clips'].remove(clip)
        return self.store.get(project_id)

    def schedule(self, project_id, clip_id=None):
        from .exposures import ordered_exposures
        project = self.store.get(project_id)
        exposures, loop = ordered_exposures(project, clip_id)
        result = []
        elapsed = 0
        for item in exposures:
            frame = find_frame(project, item['frame_id'])
            duration = item['duration']
            result.append({'id': frame['id'], 'name': frame['name'], 'start': elapsed,
                           'end': elapsed + duration, 'duration': duration})
            elapsed += duration
        return {'frames': result, 'total_duration': elapsed, 'loop': loop}
