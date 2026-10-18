"""Prepare a pinned, isolated PDF runtime; never install into EzRead's .venv."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pdf_layout_engine as engine  # noqa: E402


def checked_target(path):
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()) or path == ROOT.resolve():
        raise ValueError('Runtime target must remain inside the project.')
    return path


def run_json(executable, arguments, env):
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run([str(executable), *arguments], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, text=True, encoding='utf-8', check=True, **flags)
    return json.loads(result.stdout)


def seed_dependencies(seed, runtime, executable, env):
    seed = seed.resolve()
    seed_python = seed / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    # Only installed distribution files are copied. No HOME, account settings,
    # scripts, credentials, or arbitrary venv siblings are included.
    script = '''import importlib.metadata as m,json,pathlib,sys
pins=[line.strip().split('==') for line in pathlib.Path(sys.argv[1]).read_text().splitlines() if line.strip() and not line.startswith('#')]
files=set()
for name,version in pins:
    dist=m.distribution(name)
    if dist.version!=version: raise ValueError(f'Version mismatch: {name}: {dist.version} != {version}')
    for file in dist.files or []:
        p=pathlib.PurePosixPath(str(file).replace('\\\\','/'))
        if not p.is_absolute() and '..' not in p.parts and p.suffix!='.pyc' and '__pycache__' not in p.parts: files.add(str(p))
print(json.dumps(sorted(files)))'''
    files = run_json(seed_python, ['-B', '-c', script, str(ROOT / 'requirements-pdf-layout.txt')], env)
    source_packages = seed / ('Lib/site-packages' if os.name == 'nt' else
                              f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    target_packages = runtime / ('Lib/site-packages' if os.name == 'nt' else
                                 f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    for filename in files:
        source = (source_packages / filename).resolve()
        target = checked_target(target_packages / filename)
        if not source.is_relative_to(source_packages.resolve()) or not source.is_file():
            raise ValueError('Seed dependency file is outside site-packages or missing.')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    print(f'Copied {len(files)} public dependency files into the isolated runtime.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-runtime', type=Path)
    parser.add_argument('--seed-assets', type=Path,
                        help='BabelDOC isolated HOME containing .cache/babeldoc public assets.')
    args = parser.parse_args()
    base, executable = engine._paths(ROOT)
    base, runtime = checked_target(base), checked_target(base / 'runtime')
    env = engine.runtime_environment(ROOT)
    if not executable.is_file():
        venv.EnvBuilder(with_pip=not bool(args.seed_runtime)).create(runtime)
    if args.seed_runtime:
        if args.seed_runtime.resolve() == runtime:
            raise ValueError('Seed runtime must differ from the target runtime.')
        seed_dependencies(args.seed_runtime, runtime, executable, env)
    else:
        flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        has_pip = subprocess.run([str(executable), '-c',
                                  "import importlib.util;raise SystemExit(0 if importlib.util.find_spec('pip') else 1)"],
                                 cwd=ROOT, env=env, **flags).returncode == 0
        if not has_pip:
            subprocess.run([str(executable), '-m', 'ensurepip', '--upgrade'],
                           cwd=ROOT, env=env, check=True, **flags)
        subprocess.run([str(executable), '-m', 'pip', 'install', '-r',
                        str(ROOT / 'requirements-pdf-layout.txt')], cwd=ROOT,
                       env=env, check=True, **flags)
    if args.seed_assets:
        official = run_json(executable, [str(ROOT / 'pdf_layout_engine.py'), '--assets-list'], env)
        source_cache = args.seed_assets.resolve() / '.cache' / 'babeldoc'
        target_cache = checked_target(base / 'home' / '.cache' / 'babeldoc')
        for folder, entries in official.items():
            for entry in entries:
                source = (source_cache / folder / entry['name']).resolve()
                target = checked_target(target_cache / folder / entry['name'])
                if not source.is_relative_to(source_cache.resolve()) or not source.is_file():
                    raise ValueError(f'Missing seed public asset: {folder}/{entry["name"]}')
                import hashlib
                if hashlib.sha3_256(source.read_bytes()).hexdigest() != entry['sha3_256']:
                    raise ValueError(f'Seed asset does not match official checksum: {folder}/{entry["name"]}')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        arguments = ['--probe']
    else:
        arguments = ['--prepare-assets']
    manifest = run_json(executable, [str(ROOT / 'pdf_layout_engine.py'), *arguments], env)
    engine._write(base / 'runtime-manifest.json', manifest)
    print(json.dumps(engine.availability(ROOT)), flush=True)


if __name__ == '__main__':
    main()
