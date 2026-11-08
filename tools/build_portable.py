"""Build the portable Windows ZIP from an explicit, reviewable manifest.

The public repository intentionally contains more than the release does. Tests, build tools,
developer utilities and validation notes stay browsable in source control, but they are not
shipped to people who only want to read manuals. Nothing is added to the package unless it
appears in the manifest below.
"""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PREFIX = 'Field-Manual-Viewer/'

# Documents and launchers a user needs.
ROOT_FILES = (
    'Start-Field-Manual-Viewer.bat',
    'Set-Up-Library.bat',
    'Library-Setup.ps1',
    'README.md',
    'LICENSE',
    'THIRD-PARTY.md',
)

# Application modules reachable at runtime, plus the browser interface.
PACKAGE_FILES = (
    '__init__.py',
    '__main__.py',
    'campaign.py',
    'catalog.py',
    'collect.py',
    'core.py',
    'discover.py',
    'download.py',
    'indexing.py',
    'launcher.py',
    'lifecycle.py',
    'network.py',
    'notifier.py',
    'parallel.py',
    'parallel_network.py',
    'readers.py',
    'report.py',
    'search.py',
    'server.py',
    'server_app.py',
    'setup_control.py',
    'setup_launcher.py',
    'source.py',
    'viewer_lifetime.py',
    'static/app.js',
    'static/index.html',
    'static/style.css',
)

# Public source only. Neither module is imported by user-facing code: acceptance.py is an
# offline acceptance harness and repair_metadata.py is an offline catalog-repair helper.
DEV_ONLY_PACKAGE_FILES = ('acceptance.py', 'repair_metadata.py')

FORBIDDEN_SUFFIXES = ('.pdf', '.epub', '.sqlite', '.sqlite-wal', '.sqlite-shm')
FORBIDDEN_TOP_LEVEL = ('data', 'archive', 'reports', 'tmp', '.git', 'tests', 'tools', 'docs')


def runtime_files(runtime):
    return sorted(
        p for p in runtime.rglob('*')
        if p.is_file()
        and '__pycache__' not in p.parts
        and p.suffix != '.pyc'
        and p.name != '.setup-complete'
        and 'Scripts' not in p.relative_to(runtime).parts
    )


def build():
    runtime = ROOT / 'runtime/python'
    if not (runtime / 'python.exe').exists():
        raise RuntimeError('Prepare the private runtime first with tools/Bootstrap-Runtime.ps1')

    package = ROOT / 'field_manual_viewer'
    for name in DEV_ONLY_PACKAGE_FILES:
        if not (package / name).is_file():
            raise RuntimeError('Expected developer-only source file is missing: ' + name)

    manifest = [ROOT / name for name in ROOT_FILES]
    manifest += [package / name for name in PACKAGE_FILES]
    missing = [str(p.relative_to(ROOT)) for p in manifest if not p.is_file()]
    if missing:
        raise RuntimeError('Manifest entries are missing: ' + ', '.join(missing))

    files = manifest + runtime_files(runtime)
    expected = {PREFIX + p.relative_to(ROOT).as_posix() for p in files}

    target = ROOT / 'build/Field-Manual-Viewer-Windows-x64.zip'
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            if rel.split('/')[0] in FORBIDDEN_TOP_LEVEL:
                raise RuntimeError('Private library data cannot be packaged: ' + rel)
            if path.name.lower().endswith(FORBIDDEN_SUFFIXES):
                raise RuntimeError('Documents and databases cannot be packaged: ' + rel)
            name = PREFIX + rel
            if path.name.endswith('._pth'):
                archive.writestr(name, 'python314.zip\n.\nLib\\site-packages\n..\\..\nimport site\n')
            else:
                archive.write(path, name)

    verify(target, expected)
    sha = hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()
    target.with_suffix('.zip.sha256').write_text(sha + '  ' + target.name + '\n', encoding='ascii')
    print(json.dumps({'path': str(target), 'bytes': target.stat().st_size,
                      'sha256': sha, 'files': len(expected)}))
    return target


def verify(target, expected):
    """Check the actual archive bytes, not just the intent of the writer."""
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        if archive.testzip() is not None:
            raise RuntimeError('The compressed archive failed its integrity test')
    if len(names) != len(set(names)):
        raise RuntimeError('Duplicate entries in the archive')
    unexpected = sorted(set(names) - expected)
    absent = sorted(expected - set(names))
    if unexpected:
        raise RuntimeError('Unexpected archive entries: ' + ', '.join(unexpected[:5]))
    if absent:
        raise RuntimeError('Missing archive entries: ' + ', '.join(absent[:5]))
    for name in names:
        if not name.startswith(PREFIX):
            raise RuntimeError('Entry outside the package folder: ' + name)
        if '..' in Path(name).parts:
            raise RuntimeError('Entry escapes the extraction directory: ' + name)
        rel = name[len(PREFIX):]
        if rel.split('/')[0] in FORBIDDEN_TOP_LEVEL:
            raise RuntimeError('Private library directory in the archive: ' + name)
        if name.lower().endswith(FORBIDDEN_SUFFIXES):
            raise RuntimeError('Document or database in the archive: ' + name)


if __name__ == '__main__':
    build()
