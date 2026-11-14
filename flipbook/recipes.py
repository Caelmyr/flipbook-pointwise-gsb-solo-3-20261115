from copy import deepcopy
from . import validate as v
from .model import new_id
from .pixels import apply_effect
from PIL import Image


class Recipes:
    def __init__(self, store):
        self.store = store
        self.directory = store.root / 'recipes'
        self.directory.mkdir(exist_ok=True)

    def validate(self, body):
        name = v.text(body.get('name'), '配方名称', 120, False)
        description = v.text(body.get('description', ''), '配方说明', 2000)
        steps = v.array(body.get('steps'), '像素处理步骤', 12)
        if not steps:
            raise v.Invalid('配方至少包含一步')
        probe = Image.new('RGBA', (32, 32), '#65e2b9ff')
        for step in steps:
            v.object_value(step, '处理步骤')
            probe = apply_effect(probe, step)
        return {'name': name, 'description': description, 'steps': deepcopy(steps)}

    def create(self, body):
        recipe = self.validate(body)
        recipe.update({'id': new_id(), 'revision': 0, 'created_at': v.timestamp(), 'updated_at': v.timestamp()})
        with self.store.lock:
            self.store.write_json(self.directory / (recipe['id'] + '.json'), recipe)
        return recipe

    def get(self, recipe_id):
        v.key(recipe_id)
        with self.store.lock:
            result = self.store.read_json(self.directory / (recipe_id + '.json'))
        if result is None:
            raise v.Invalid('像素配方不存在')
        return result

    def list(self):
        with self.store.lock:
            result = [self.store.read_json(path) for path in self.directory.glob('*.json')]
        return sorted(result, key=lambda r: r['updated_at'], reverse=True)

    def update(self, recipe_id, body):
        validated = self.validate(body)
        with self.store.lock:
            original = self.get(recipe_id)
            if original['revision'] != v.revision(body):
                raise v.Conflict('配方已修改，请刷新')
            original.update(validated)
            original['revision'] += 1
            original['updated_at'] = v.timestamp()
            self.store.write_json(self.directory / (recipe_id + '.json'), original)
        return original

    def apply(self, project_id, revision, recipe_id, frame_ids):
        from .pipelines import effect_pipeline
        recipe = self.get(recipe_id)
        return effect_pipeline(self.store, project_id, revision, {'steps': recipe['steps'], 'frame_ids': frame_ids})

    def export(self, recipe_id):
        recipe = self.get(recipe_id)
        return {
            'format': 'flipbook-pixel-recipe',
            'version': 1,
            'recipe': {key: recipe[key] for key in ['name', 'description', 'steps']},
        }

    def import_recipe(self, body):
        if body.get('format') != 'flipbook-pixel-recipe' or body.get('version') != 1:
            raise v.Invalid('不支持的像素配方格式')
        return self.create(v.object_value(body.get('recipe'), '配方'))

    def ensure_defaults(self):
        marker = self.directory / 'defaults.marker'
        if marker.exists():
            return
        defaults = [
            {'name': '复古四色', 'steps': [{'operation': 'posterize', 'levels': 4}]},
            {'name': '清晰像素描边', 'steps': [{'operation': 'alpha_threshold', 'threshold': 128}, {'operation': 'outline', 'radius': 1, 'color': '#152437ff'}]},
            {'name': '柔和荧光', 'steps': [{'operation': 'saturation', 'amount': 1.2}, {'operation': 'shadow', 'dx': 0, 'dy': 0, 'radius': 3, 'color': '#809affcc'}]},
            {'name': '灰阶高对比', 'steps': [{'operation': 'grayscale'}, {'operation': 'contrast', 'amount': 1.4}]},
        ]
        with self.store.lock:
            for body in defaults:
                self.create(body)
            marker.write_text('1')
