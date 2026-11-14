from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
target = root / 'flipbook-source.zip'
excluded = {'.venv', '__pycache__', '.git', 'data', '.pytest_cache'}
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if not path.is_file() or set(relative.parts).intersection(excluded):
            continue
        if path == target or path.suffix in ('.pyc', '.log', '.zip'):
            continue
        archive.write(path, 'solo_5/' + relative.as_posix())
print(target)
