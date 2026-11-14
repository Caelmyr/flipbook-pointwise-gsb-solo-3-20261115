"""Resolve clip exposure independently of the editable cel's hold length."""
from . import validate as v
from .model import find_frame


def clip_exposures(project, clip):
    source = clip.get('exposures')
    if source is None:
        source = [{'frame_id': key, 'duration': find_frame(project, key)['duration']}
                  for key in clip['frame_ids']]
    return [dict(item) for item in source]


def ordered_exposures(project, clip_id=None, frame_ids=None):
    loop = project['loop']
    if clip_id:
        clip = next((item for item in project['clips'] if item['id'] == clip_id), None)
        if clip is None:
            raise v.Invalid('片段不存在')
        items = clip_exposures(project, clip)
        if clip['direction'] == 'reverse':
            items.reverse()
        elif clip['direction'] == 'pingpong':
            items += [dict(item) for item in items[-2:0:-1]]
        loop = clip['repeat']
    else:
        ids = v.frame_ids(frame_ids if frame_ids is not None else
                          [frame['id'] for frame in project['frames']], project)
        items = [{'frame_id': key, 'duration': find_frame(project, key)['duration']}
                 for key in ids]
    return items, loop
