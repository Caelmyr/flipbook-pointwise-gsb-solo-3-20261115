from concurrent.futures import ThreadPoolExecutor
from threading import Event
from copy import deepcopy
from . import validate as v
from .model import new_id
from .exporting import animated_gif, animated_png, animated_webp, png_sequence, atlas_zip, sheet_zip, project_bundle, palette_files


FORMATS = {
    'gif': ('animation.gif', 'image/gif'),
    'multipage': ('atlas-pages.zip', 'application/zip'),
    'apng': ('animation.png', 'image/png'),
    'webp': ('animation.webp', 'image/webp'),
    'sequence': ('png-sequence.zip', 'application/zip'),
    'atlas': ('atlas.zip', 'application/zip'),
    'sheet': ('sheet.zip', 'application/zip'),
    'project': ('project.zip', 'application/zip'),
    'palette': ('palette.zip', 'application/zip'),
}


class Jobs:
    def __init__(self, store):
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='flipbook-export')
        self.events = {}
        self.futures = {}
        with store.lock:
            for path in store.jobs.glob('*.json'):
                job = store.read_json(path)
                if job['status'] in ('queued', 'running', 'cancelling'):
                    job['status'] = 'interrupted'
                    job['error'] = '服务重启中断，可按原参数重试'
                    job['finished_at'] = v.timestamp()
                    store.write_json(path, job)

    def get(self, job_id):
        v.key(job_id)
        with self.store.lock:
            job = self.store.read_json(self.store.jobs / (job_id + '.json'))
        if job is None:
            raise v.Invalid('导出任务不存在')
        return job

    def save(self, job):
        job['updated_at'] = v.timestamp()
        self.store.write_json(self.store.jobs / (job['id'] + '.json'), job)

    def list(self, project_id=None):
        with self.store.lock:
            jobs = [self.store.read_json(path) for path in self.store.jobs.glob('*.json')]
        if project_id:
            jobs = [job for job in jobs if job['project_id'] == project_id]
        return sorted(jobs, key=lambda j: j['created_at'], reverse=True)

    def start(self, project_id, body):
        kind = v.choice(body.get('format', 'apng'), '导出格式', FORMATS)
        with self.store.lock:
            active = [job for job in self.list() if job['status'] in ('queued', 'running', 'cancelling')]
            if len(active) >= 5:
                raise v.Invalid('最多排队5个导出任务')
            snapshot = self.store.get(project_id)
            if 'revision' in body and v.revision(body) != snapshot['revision']:
                raise v.Conflict('项目已修改，请刷新后导出')
            job_id = new_id()
            now = v.timestamp()
            job = {
                'id': job_id,
                'project_id': project_id,
                'project_name': snapshot['name'],
                'source_revision': snapshot['revision'],
                'format': kind,
                'parameters': deepcopy(body),
                'status': 'queued',
                'stage': '等待编码',
                'created_at': now,
                'updated_at': now,
                'finished_at': None,
                'filename': FORMATS[kind][0],
                'mimetype': FORMATS[kind][1],
                'bytes': 0,
                'details': None,
                'error': None,
            }
            self.save(job)
            event = Event()
            self.events[job_id] = event
            self.futures[job_id] = self.executor.submit(self.run, job_id, snapshot, event)
        return self.get(job_id)

    def run(self, job_id, project, event):
        try:
            with self.store.lock:
                job = self.get(job_id)
                if event.is_set():
                    job['status'] = 'cancelled'
                    job['finished_at'] = v.timestamp()
                    self.save(job)
                    return
                job['status'] = 'running'
                job['stage'] = '渲染与编码（当前阶段不可中断）'
                self.save(job)
            body = job['parameters']
            details = {}
            if job['format'] == 'gif':
                data, details = animated_gif(self.store, project, body)
            elif job['format'] == 'apng':
                data, details = animated_png(self.store, project, body)
            elif job['format'] == 'webp':
                data, details = animated_webp(self.store, project, body)
            elif job['format'] == 'sequence':
                data = png_sequence(self.store, project, body)
            elif job['format'] == 'atlas':
                data, details = atlas_zip(self.store, project, body)
            elif job['format'] == 'multipage':
                from .multipage import multipage_atlas
                data, details = multipage_atlas(self.store, project, body)
            elif job['format'] == 'sheet':
                data, details = sheet_zip(self.store, project, body)
            elif job['format'] == 'palette':
                data = palette_files(project)
            else:
                data = project_bundle(self.store, project)
            with self.store.lock:
                job = self.get(job_id)
                if event.is_set():
                    job['status'] = 'cancelled'
                    job['stage'] = '已丢弃取消任务的输出'
                else:
                    path = self.store.jobs / (job_id + '.bin')
                    temporary = self.store.jobs / (job_id + '.tmp')
                    temporary.write_bytes(data)
                    temporary.replace(path)
                    job['status'] = 'completed'
                    job['stage'] = '可下载'
                    job['bytes'] = len(data)
                    job['details'] = details
                job['finished_at'] = v.timestamp()
                self.save(job)
        except Exception as error:
            with self.store.lock:
                job = self.get(job_id)
                job['status'] = 'failed'
                job['error'] = str(error)
                job['stage'] = '导出失败'
                job['finished_at'] = v.timestamp()
                self.save(job)

    def cancel(self, job_id):
        with self.store.lock:
            job = self.get(job_id)
            if job['status'] not in ('queued', 'running', 'cancelling'):
                raise v.Invalid('任务已结束')
            self.events[job_id].set()
            job['status'] = 'cancelling'
            job['stage'] = '编码阶段结束后丢弃输出'
            self.save(job)
        return self.get(job_id)

    def retry(self, job_id):
        job = self.get(job_id)
        if job['status'] not in ('failed', 'interrupted', 'cancelled'):
            raise v.Invalid('只有未完成的任务可重试')
        body = dict(job['parameters'])
        body.pop('revision', None)
        return self.start(job['project_id'], body)

    def download(self, job_id):
        job = self.get(job_id)
        if job['status'] != 'completed':
            raise v.Invalid('导出尚未完成')
        path = self.store.jobs / (job_id + '.bin')
        if not path.exists():
            raise v.Invalid('导出文件缺失，请重新导出')
        return path, job
