from .layer_nodes import walk_layers
import hashlib
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from copy import deepcopy
from PIL import Image
from . import validate as v
from .model import new_id, new_project, project_snapshot, validate_structure, public_summary


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.projects = self.root / 'projects'
        self.jobs = self.root / 'jobs'
        self.lock = RLock()
        self.projects.mkdir(parents=True, exist_ok=True)
        self.jobs.mkdir(exist_ok=True)

    def read_json(self, path, default=None):
        if not path.exists():
            return deepcopy(default)
        try:
            return json.loads(path.read_text(encoding='utf8'))
        except (OSError, json.JSONDecodeError) as error:
            raise v.Invalid('数据读取失败：' + path.name) from error

    def write_json(self, path, value):
        temporary = path.with_name(path.name + '.' + new_id() + '.tmp')
        try:
            with temporary.open('w', encoding='utf8') as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def directory(self, project_id):
        v.key(project_id)
        path = self.projects / project_id
        if not (path / 'project.json').exists():
            raise v.Invalid('项目不存在')
        return path

    def get(self, project_id):
        with self.lock:
            project = self.read_json(self.directory(project_id) / 'project.json')
            return project

    def list(self, query='', archived=False):
        result = []
        with self.lock:
            for path in self.projects.glob('*/project.json'):
                project = self.read_json(path)
                if project['archived'] and not archived:
                    continue
                searchable = ' '.join([project['name'], project['notes'], *project['tags']])
                if query and query.casefold() not in searchable.casefold():
                    continue
                result.append(public_summary(project))
        return sorted(result, key=lambda p: p['updated_at'], reverse=True)

    def create(self, name, width, height):
        name = v.text(name, '项目名称', 120, False)
        width, height = v.dimensions({'width': width, 'height': height})
        with self.lock:
            project_id = new_id()
            path = self.projects / project_id
            path.mkdir()
            for child in ['assets', 'history', 'exports']:
                (path / child).mkdir()
            blank = Image.new('RGBA', (width, height))
            asset = self.put_image_at(path, blank)
            project = new_project(name, width, height, asset)
            project['id'] = project_id
            self.write_json(path / 'project.json', project)
            return project

    def put_image_at(self, directory, image):
        image = image.convert('RGBA')
        if image.width * image.height > 4194304:
            raise v.Invalid('单张图层不能超过2048×2048像素预算')
        header = f'{image.width}x{image.height}:RGBA:'.encode()
        asset_id = hashlib.sha256(header + image.tobytes()).hexdigest()
        target = directory / 'assets' / (asset_id + '.png')
        if not target.exists():
            stream = io.BytesIO()
            image.save(stream, format='PNG')
            temporary = target.with_name(asset_id + '.' + new_id() + '.tmp')
            try:
                temporary.write_bytes(stream.getvalue())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return asset_id

    def put_image(self, project_id, image):
        return self.put_image_at(self.directory(project_id), image)

    def image(self, project_id, asset_id):
        v.digest(asset_id)
        target = self.directory(project_id) / 'assets' / (asset_id + '.png')
        if not target.exists():
            raise v.Invalid('图层像素文件缺失')
        with Image.open(target) as image:
            return image.convert('RGBA')

    @contextmanager
    def edit(self, project_id, revision, operation):
        with self.lock:
            path = self.directory(project_id)
            original = self.get(project_id)
            if revision != original['revision']:
                raise v.Conflict('项目已被其他操作修改，请刷新后重试')
            project = deepcopy(original)
            yield project
            validate_structure(project)
            history_id = new_id()
            entry = {
                'id': history_id,
                'operation': operation,
                'created_at': v.timestamp(),
                'before': project_snapshot(original),
                'after': project_snapshot(project),
            }
            self.write_json(path / 'history' / (history_id + '.json'), entry)
            project['undo'] = (original['undo'] + [history_id])[-100:]
            project['redo'] = []
            project['revision'] = original['revision'] + 1
            project['updated_at'] = v.timestamp()
            self.write_json(path / 'project.json', project)

    def undo(self, project_id, revision):
        with self.lock:
            project = self.get(project_id)
            if revision != project['revision']:
                raise v.Conflict('项目已修改，请刷新')
            if not project['undo']:
                raise v.Invalid('没有可撤销操作')
            entry_id = project['undo'][-1]
            path = self.directory(project_id)
            entry = self.read_json(path / 'history' / (entry_id + '.json'))
            restored = deepcopy(entry['before'])
            restored['revision'] = project['revision'] + 1
            restored['updated_at'] = v.timestamp()
            restored['undo'] = project['undo'][:-1]
            restored['redo'] = project['redo'] + [entry_id]
            self.write_json(path / 'project.json', restored)
            return restored

    def redo(self, project_id, revision):
        with self.lock:
            project = self.get(project_id)
            if revision != project['revision']:
                raise v.Conflict('项目已修改，请刷新')
            if not project['redo']:
                raise v.Invalid('没有可重做操作')
            entry_id = project['redo'][-1]
            path = self.directory(project_id)
            entry = self.read_json(path / 'history' / (entry_id + '.json'))
            restored = deepcopy(entry['after'])
            restored['revision'] = project['revision'] + 1
            restored['updated_at'] = v.timestamp()
            restored['undo'] = project['undo'] + [entry_id]
            restored['redo'] = project['redo'][:-1]
            self.write_json(path / 'project.json', restored)
            return restored

    def history(self, project_id):
        project = self.get(project_id)
        result = []
        for entry_id in project['undo']:
            entry = self.read_json(self.directory(project_id) / 'history' / (entry_id + '.json'))
            result.append({key: entry[key] for key in ['id', 'operation', 'created_at']})
        return result[::-1]

    def update(self, project_id, revision, body):
        with self.edit(project_id, revision, '项目设置') as project:
            if 'name' in body:
                project['name'] = v.text(body['name'], '项目名称', 120, False)
            if 'notes' in body:
                project['notes'] = v.text(body['notes'], '说明', 5000)
            if 'tags' in body:
                project['tags'] = list(dict.fromkeys(v.text(item, '标签', 30, False) for item in v.array(body['tags'], '标签', 20)))
            if 'loop' in body:
                project['loop'] = v.integer(body['loop'], '循环次数', 0, 65535)
            if 'archived' in body:
                project['archived'] = v.boolean(body['archived'], '归档')
            if 'background' in body:
                project['background'] = v.hex_color(body['background'])
            if 'palette' in body:
                project['palette'] = v.palette(body['palette'])
        return self.get(project_id)

    def duplicate(self, project_id):
        import shutil
        with self.lock:
            original = self.get(project_id)
            copied_id = new_id()
            target = self.projects / copied_id
            shutil.copytree(self.directory(project_id), target, ignore=shutil.ignore_patterns('exports', 'history'))
            (target / 'exports').mkdir()
            (target / 'history').mkdir()
            original['id'] = copied_id
            original['name'] = original['name'][:110] + ' · 副本'
            original['undo'] = []
            original['redo'] = []
            original['revision'] = 0
            original['archived'] = False
            original['created_at'] = v.timestamp()
            original['updated_at'] = v.timestamp()
            self.write_json(target / 'project.json', original)
            return original

    def asset_inventory(self, project_id):
        project = self.get(project_id)
        active = {asset for frame in project['frames'] for layer in walk_layers(frame['layers'])
                  for asset in [layer['asset'], layer.get('role_legacy_asset'), layer.get('mask')] if asset}
        from .atlas_workspaces import asset_sizes
        active.update(asset_sizes(project))
        all_assets = list((self.directory(project_id) / 'assets').glob('*.png'))
        return {
            'active_assets': len(active),
            'stored_assets': len(all_assets),
            'bytes': sum(path.stat().st_size for path in all_assets),
            'history_entries': len(list((self.directory(project_id) / 'history').glob('*.json'))),
            'note': '保留不可变像素文件供撤销与历史引用，不自动删除',
        }
