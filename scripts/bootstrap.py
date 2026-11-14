import hashlib
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if sys.version_info < (3, 12):
    raise SystemExit('需要Python 3.12或更新版本；推荐直接使用Docker Compose。')
requirements = root / 'requirements.txt'
fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
marker = Path(sys.prefix) / '.flipbook-requirements'
if not marker.exists() or marker.read_text() != fingerprint:
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(requirements)], check=True)
    marker.write_text(fingerprint)
