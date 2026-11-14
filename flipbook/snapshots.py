from .layer_nodes import walk_layers
from copy import deepcopy
from . import validate as v
from .model import new_id, project_snapshot, validate_structure


class Snapshots:
    def __init__(self, store):
        self.store = store

    def directory(self, project_id):
        path = self.store.directory(project_id) / 'snapshots'
        path.mkdir(exist_ok=True)
        return path

    def create(self, project_id, body):
        name = v.text(body.get('name'), '版本名称', 120, False)
        description = v.text(body.get('description', ''), '版本说明', 2000)
        with self.store.lock:
            project = self.store.get(project_id)
            if 'revision' in body and v.revision(body) != project['revision']:
                raise v.Conflict('项目已修改，请刷新后保存版本')
            directory = self.directory(project_id)
            if len(list(directory.glob('*.json'))) >= 100:
                raise v.Invalid('最多保存100个命名版本')
            snapshot = {
                'id': new_id(),
                'name': name,
                'description': description,
                'created_at': v.timestamp(),
                'source_revision': project['revision'],
                'project': project_snapshot(project),
            }
            self.store.write_json(directory / (snapshot['id'] + '.json'), snapshot)
            return self.summary(snapshot)

    def summary(self, snapshot):
        project = snapshot['project']
        return {
            'id': snapshot['id'],
            'name': snapshot['name'],
            'description': snapshot['description'],
            'created_at': snapshot['created_at'],
            'source_revision': snapshot['source_revision'],
            'width': project['width'],
            'height': project['height'],
            'frames': len(project['frames']),
            'duration': sum(frame['duration'] for frame in project['frames']),
            'layer_count': sum(len(frame['layers']) for frame in project['frames']),
        }

    def get(self, project_id, snapshot_id):
        v.key(snapshot_id)
        path = self.directory(project_id) / (snapshot_id + '.json')
        result = self.store.read_json(path)
        if result is None:
            raise v.Invalid('命名版本不存在')
        return result

    def list(self, project_id):
        with self.store.lock:
            result = [self.summary(self.store.read_json(path)) for path in self.directory(project_id).glob('*.json')]
        return sorted(result, key=lambda s: s['created_at'], reverse=True)

    def restore(self, project_id, revision, snapshot_id):
        with self.store.edit(project_id, revision, '恢复命名版本') as project:
            snapshot = self.get(project_id, snapshot_id)
            restored = deepcopy(snapshot['project'])
            validate_structure(restored)
            for key, value in restored.items():
                if key not in ('id', 'revision', 'undo', 'redo', 'created_at', 'updated_at'):
                    project[key] = value
        return self.store.get(project_id)

    def fork(self, project_id, snapshot_id):
        with self.store.lock:
            snapshot = self.get(project_id, snapshot_id)
            original = snapshot['project']
            created = self.store.create(original['name'][:95] + ' · 版本分支', original['width'], original['height'])
            with self.store.edit(created['id'], 0, '从命名版本创建项目') as project:
                for key, value in original.items():
                    if key not in ('id', 'revision', 'undo', 'redo', 'created_at', 'updated_at'):
                        project[key] = deepcopy(value)
                for frame in project['frames']:
                    for layer in walk_layers(frame['layers']):
                        image = self.store.image(project_id, layer['asset'])
                        layer['asset'] = self.store.put_image(created['id'], image)
                        if layer.get('role_legacy_asset'):
                            fallback = self.store.image(project_id, layer['role_legacy_asset'])
                            layer['role_legacy_asset'] = self.store.put_image(created['id'], fallback)
                from .atlas_workspaces import workspaces
                for workspace in workspaces(project):
                    for version in workspace['versions']:
                        page = self.store.image(project_id, version['page_asset'])
                        version['page_asset'] = self.store.put_image(created['id'], page)
                from .lighting import copy_referenced
                copy_referenced(self.store, project_id, created['id'], project)
            return self.store.get(created['id'])

    def compare(self, project_id, first_id, second_id):
        first = self.get(project_id, first_id)['project']
        second = self.get(project_id, second_id)['project']
        first_frames = {frame['id']: frame for frame in first['frames']}
        second_frames = {frame['id']: frame for frame in second['frames']}
        added = [key for key in second_frames if key not in first_frames]
        removed = [key for key in first_frames if key not in second_frames]
        changed = []
        for key in first_frames.keys() & second_frames.keys():
            a, b = first_frames[key], second_frames[key]
            if a != b:
                changed.append({
                    'id': key,
                    'name': b['name'],
                    'duration_before': a['duration'],
                    'duration_after': b['duration'],
                    'layers_before': len(a['layers']),
                    'layers_after': len(b['layers']),
                    'pixel_assets_changed': [l['asset'] for l in a['layers']] != [l['asset'] for l in b['layers']],
                })
        return {
            'added_frame_ids': added,
            'removed_frame_ids': removed,
            'changed_frames': changed,
            'canvas_before': [first['width'], first['height']],
            'canvas_after': [second['width'], second['height']],
            'palette_changed': first['palette'] != second['palette'],
            'ink_roles_changed': first.get('ink_roles') != second.get('ink_roles'),
            'clips_changed': first['clips'] != second['clips'],
            'timeline_order_changed': list(first_frames) != list(second_frames),
        }
