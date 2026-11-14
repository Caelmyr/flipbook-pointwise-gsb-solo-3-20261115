import hashlib
import io
import json
import sys
import time
import urllib.request
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else 'http://127.0.0.1:8895'
PHASE = sys.argv[1] if len(sys.argv) > 1 else 'before'


def request(path, body=None):
    raw = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=raw, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read()
        return json.loads(data) if 'application/json' in response.headers.get('Content-Type', '') else data


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


health = request('/api/health')
assert health['status'] == 'ok'
if PHASE == 'before':
    project = request('/api/demo', {'kind': 'flame'})
    key = project['id']
    image = request(f'/api/projects/{key}/frames/{project["frames"][0]["id"]}/image')
    job = request(f'/api/projects/{key}/jobs', {'format': 'apng', 'revision': project['revision']})
    for _ in range(100):
        job = request('/api/jobs/' + job['id'])
        if job['status'] in ('completed', 'failed'):
            break
        time.sleep(.1)
    assert job['status'] == 'completed', job
    animation = request('/api/jobs/' + job['id'] + '/download')
    with Image.open(io.BytesIO(animation)) as decoded:
        assert decoded.n_frames == 12
        duration = 0
        for index in range(decoded.n_frames):
            decoded.seek(index)
            duration += decoded.info['duration']
            assert decoded.convert('RGBA').getpixel((0, 0))[3] == 0
    assert duration == 1080
    data = {
        'health': health,
        'project_id': key,
        'project_sha256': digest(project),
        'first_frame_sha256': hashlib.sha256(image).hexdigest(),
        'export_job_id': job['id'],
        'export_sha256': hashlib.sha256(animation).hexdigest(),
        'decoded_frames': 12,
        'decoded_duration_ms': duration,
        'transparent_corner': True,
    }
    (ROOT / 'docs/http-before.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print('容器HTTP创建、真实APNG编码及透明度验证通过')
else:
    data = json.loads((ROOT / 'docs/http-before.json').read_text())
    key = data['project_id']
    project = request('/api/projects/' + key)
    image = request(f'/api/projects/{key}/frames/{project["frames"][0]["id"]}/image')
    job = request('/api/jobs/' + data['export_job_id'])
    animation = request('/api/jobs/' + data['export_job_id'] + '/download')
    assert digest(project) == data['project_sha256']
    assert hashlib.sha256(image).hexdigest() == data['first_frame_sha256']
    assert hashlib.sha256(animation).hexdigest() == data['export_sha256']
    assert job['status'] == 'completed'
    data['restart_project_equal'] = True
    data['restart_pixels_equal'] = True
    data['restart_export_equal'] = True
    data['restart_completed_job_retained'] = True
    data['health_after_restart'] = health
    (ROOT / 'docs/container-results.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print('实际容器重启后：项目JSON、图像、导出文件SHA256与任务完成状态全部一致')
