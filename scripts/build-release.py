"""Build an offline Windows payload from audited source and installed distributions."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = '3.12.10'
PYTHON_SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'


def relative_path(value):
    """Accept portable relative file names, including on non-Windows test hosts."""
    text = str(value).replace('\\', '/')
    path = PurePosixPath(text)
    if (not text or path.is_absolute() or PureWindowsPath(text).drive
            or '..' in path.parts or ':' in text or '\x00' in text):
        raise ValueError(f'Unsafe relative path: {value}')
    return path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def checked_file(base, name):
    path = (base / relative_path(name)).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError(f'Missing or escaped input file: {name}')
    return path


def copy_file(base, name, target):
    source = checked_file(base, name)
    destination = target / relative_path(name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def source_allowed(name):
    path = relative_path(name)
    if '__pycache__' in path.parts or path.suffix in {'.pyc', '.pyo'}:
        return False
    if len(path.parts) == 1:
        return path.suffix in {'.py', '.pyw'} or name in {
            'requirements.txt', 'requirements-pdf-layout.txt', 'LICENSE', 'README.md',
            'CONTRIBUTING.md', 'JOURNAL_TIERS.md', 'THIRD-PARTY-NOTICES.md'}
    return (path.parts[0] in {'ezread', 'static'}
            or name.startswith(('skills/ezread-paper-import/', 'assets/dictionaries/',
                                'assets/third-party/'))
            or name in {'docs/AI_API.md', 'docs/ARCHITECTURE.md', 'docs/PDF_LAYOUT.md',
                        'docs/INSTALLER.md'})


def tracked_sources(root):
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root,
                            check=True, stdout=subprocess.PIPE)
    ignored = subprocess.run(['git', 'ls-files', '-z', '-ci', '--exclude-standard'],
                             cwd=root, check=True, stdout=subprocess.PIPE)
    excluded = set(ignored.stdout.decode('utf-8').split('\0'))
    return sorted({name for name in result.stdout.decode('utf-8').split('\0')
                   if name and name not in excluded and source_allowed(name)})


# The isolated engine already contains packaging. Query metadata in the explicit
# source site-packages rather than importing any application dependency.
METADATA_SCRIPT = r'''
import importlib.metadata as m,json,pathlib,sys
from packaging.requirements import Requirement
from packaging.markers import default_environment
from packaging.utils import canonicalize_name
site,mode,argument=sys.argv[1:]
distributions={canonicalize_name(d.metadata['Name']):d for d in m.distributions(path=[site])}
environment=default_environment()
environment.update(python_version='3.12',python_full_version='3.12.10',extra='')
if mode=='pins':
    requests=[Requirement(line.strip()) for line in pathlib.Path(argument).read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if any(len(list(r.specifier))!=1 or any(s.operator!='==' or '*' in s.version for s in r.specifier) for r in requests): raise ValueError('Expected exact pins')
else: requests=[Requirement(name) for name in json.loads(argument)]
queue=list(requests); selected={}; visited=set()
while queue:
    requirement=queue.pop(0)
    if requirement.marker and not requirement.marker.evaluate(environment): continue
    name=canonicalize_name(requirement.name); dist=distributions[name]
    if requirement.specifier and not requirement.specifier.contains(dist.version,prereleases=True): raise ValueError('Installed version differs: '+name)
    visit=(name,tuple(sorted(requirement.extras)))
    if visit in visited: continue
    visited.add(visit)
    selected[name]={'name':dist.metadata['Name'],'version':dist.version,'files':sorted(str(f).replace('\\','/') for f in dist.files or [])}
    if not selected[name]['files']: raise ValueError('Distribution has no RECORD: '+name)
    if mode!='pins':
        for text in dist.requires or []:
            child=Requirement(text)
            if child.marker and not any(child.marker.evaluate(dict(environment,extra=extra)) for extra in (requirement.extras or {''})): continue
            child.marker=None
            queue.append(child)
print(json.dumps([selected[name] for name in sorted(selected)]))
'''


def distributions(root, mode):
    engine = root / '.deps/babeldoc/runtime'
    site = (root / '.venv' if mode == 'closure' else engine) / 'Lib/site-packages'
    argument = (json.dumps(['pdfplumber', 'pypdfium2', 'Pillow']) if mode == 'closure'
                else str(root / 'requirements-pdf-layout.txt'))
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run([str(engine / 'Scripts/python.exe'), '-I', '-B', '-c',
                             METADATA_SCRIPT, str(site), mode, argument], cwd=root,
                            check=True, stdout=subprocess.PIPE, text=True, encoding='utf-8', **flags)
    return site, json.loads(result.stdout)


def public_distribution_file(name):
    try:
        path = relative_path(name)
    except ValueError:
        return False
    return (path.suffix not in {'.pyc', '.pyo'} and '__pycache__' not in path.parts
            and path.name.lower() not in {'direct_url.json', 'origin.json'}
            and path.name not in {'INSTALLER', 'REQUESTED'})


def copy_distributions(site, packages, target, license_root):
    for package in packages:
        for name in package['files']:
            if not public_distribution_file(name):
                continue
            copy_file(site, name, target)
            path = PurePosixPath(name)
            if any(part.lower().startswith(('license', 'copying', 'notice', 'authors'))
                   for part in path.parts):
                license_name = relative_path(f"{package['name']}/{name}")
                destination = license_root / license_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(checked_file(site, name), destination)


def copy_engine_support_dll(site, scripts):
    # PyMuPDF 等在 sklearn 预加载前需要 C++ 运行库。仅复用已按 RECORD
    # 收录且附带分发许可的 wheel 文件，不从系统目录收集 DLL。
    copy_file(site / 'sklearn/.libs', 'msvcp140.dll', scripts)


def extract_python(archive, target, app_relative, site_relative):
    if sha256(archive) != PYTHON_SHA256:
        raise ValueError('Embedded CPython archive SHA256 mismatch')
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            name = relative_path(entry.filename)
            if entry.is_dir():
                continue
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Embedded archive contains a symlink')
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as stream, destination.open('wb') as output:
                shutil.copyfileobj(stream, output)
    # Windows 的嵌入解释器会错误归一化以 .. 结尾的连续父路径；
    # 显式目录分隔符使 app-root 保持正确。禁止 site 自动执行用户 .pth。
    app_directory = app_relative.rstrip('/\\') + '/'
    (target / 'python312._pth').write_text(
        f'python312.zip\n.\n{site_relative}\n{app_directory}\n', encoding='utf-8')


def copy_assets(base, target):
    manifest = json.loads((base / 'runtime-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('status') != 'verified' or not manifest.get('assets'):
        raise ValueError('BabelDOC public asset manifest is not verified')
    assets = []
    source_cache = base / 'home/.cache/babeldoc'
    for entry in manifest['assets']:
        name = str(relative_path(entry['path']))
        source = checked_file(source_cache, name)
        if source.stat().st_size != entry['size'] or sha256(source) != entry['sha256']:
            raise ValueError(f'Public asset checksum mismatch: {name}')
        copy_file(source_cache, name, target / 'home/.cache/babeldoc')
        assets.append({key: entry[key] for key in ('path', 'size', 'sha256', 'sha3_256') if key in entry})
    clean = {key: manifest[key] for key in ('version', 'status', 'pymupdf', 'onnxruntime',
                                          'bitstream_import') if key in manifest}
    clean['assets'] = assets
    write_json(target / 'runtime-manifest.json', clean)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def copy_source_materials(source, target):
    if not source.is_dir():
        raise ValueError('Third-party source materials directory is missing')
    for path in sorted(source.iterdir()):
        name = path.name.lower()
        allowed = (name.endswith(('.tar.gz', '.zip'))
                   or (name.startswith(('manifest', 'readme', 'sources', 'notice'))
                       and path.suffix.lower() in {'.json', '.md', '.txt'}))
        if allowed and path.is_file() and path.stat().st_size:
            copy_file(source, path.name, target)


def refresh_manifest(output, metadata=None, root=ROOT):
    """Refresh after the release orchestrator adds its compiled launcher."""
    output, root = output.resolve(), root.resolve()
    if output == root or not output.is_relative_to(root) or not output.is_dir():
        raise ValueError('Payload must be an existing directory inside the workspace')
    manifest_path = output / 'release-manifest.json'
    if metadata is None:
        metadata = json.loads(manifest_path.read_text(encoding='utf-8'))
    inventory = {}
    for path in sorted(output.rglob('*')):
        if path.is_file() and path != manifest_path:
            name = path.relative_to(output).as_posix()
            checked_file(output, name)
            inventory[name] = {'sha256': sha256(path), 'size': path.stat().st_size}
    metadata['files'] = inventory
    write_json(manifest_path, metadata)
    return len(inventory)


def build(output, python_zip, desktop_build, source_materials, root=ROOT):
    root, output = root.resolve(), output.resolve()
    if output == root or not output.is_relative_to(root):
        raise ValueError('Output must be a new directory inside the workspace')
    if output.exists():
        raise FileExistsError('Output already exists; it will not be overwritten')
    if sha256(python_zip) != PYTHON_SHA256:
        raise ValueError('Embedded CPython archive SHA256 mismatch')
    files = tracked_sources(root)
    main_site, main_packages = distributions(root, 'closure')
    engine_site, engine_packages = distributions(root, 'pins')
    if not (desktop_build / 'EzRead.Desktop.exe').is_file():
        raise ValueError('Built desktop host is missing')
    output.mkdir(parents=True)
    for name in files:
        copy_file(root, name, output)
    runtime = output / 'runtime'
    extract_python(python_zip, runtime, '..', './Lib/site-packages')
    engine_target = output / '.deps/babeldoc'
    extract_python(python_zip, engine_target / 'runtime/Scripts', '../../../..', '../Lib/site-packages')
    copy_distributions(main_site, main_packages, runtime / 'Lib/site-packages', output / 'licenses/main')
    copy_distributions(engine_site, engine_packages, engine_target / 'runtime/Lib/site-packages',
                       output / 'licenses/babeldoc')
    copy_engine_support_dll(engine_target / 'runtime/Lib/site-packages', engine_target / 'runtime/Scripts')
    copy_assets(root / '.deps/babeldoc', engine_target)
    for path in sorted(desktop_build.rglob('*')):
        if path.is_file() and path.suffix.lower() in {'.exe', '.dll', '.config', '.txt'}:
            copy_file(desktop_build, path.relative_to(desktop_build).as_posix(), output / 'desktop/bin')
    copy_source_materials(source_materials, output / 'licenses/sources')
    copy_file(runtime, 'LICENSE.txt', output / 'licenses')
    (output / 'licenses/LICENSE.txt').rename(output / 'licenses/PYTHON-LICENSE.txt')
    module = ast.parse((root / 'ezread/config.py').read_text(encoding='utf-8'))
    version = next(ast.literal_eval(node.value) for node in module.body
                   if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERSION'
                                                          for t in node.targets))
    packages = {kind: [{'name': p['name'], 'version': p['version']} for p in values]
                for kind, values in [('main', main_packages), ('babeldoc', engine_packages)]}
    return refresh_manifest(output, {'format': 1, 'version': version,
               'python': {'version': PYTHON_VERSION, 'archive_sha256': PYTHON_SHA256},
               'packages': packages}, root=root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--refresh-manifest', action='store_true',
                        help='Refresh an existing payload after adding the compiled launcher.')
    parser.add_argument('--python-zip', type=Path,
                        default=ROOT / '.deps/packaging/python-3.12.10-embed-amd64.zip')
    parser.add_argument('--desktop-build', type=Path, default=ROOT / 'work/desktop-build')
    parser.add_argument('--source-materials', type=Path, default=ROOT / 'work/release/sources')
    args = parser.parse_args()
    count = (refresh_manifest(args.output) if args.refresh_manifest else
             build(args.output, args.python_zip, args.desktop_build, args.source_materials))
    print(f'Built payload with {count} verified files: {args.output}')


if __name__ == '__main__':
    main()
