"""Offline release boundaries; no installed packages or personal library required."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('build_release', Path(__file__).resolve().parents[1]
                                              / 'scripts/build-release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_paths_and_source_allowlist(self):
        for name in ('../private', '/private', 'C:/private', 'C:private', '\\\\host\\share',
                     'normal/../../private', 'private:stream'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.relative_path(name)
        for name in ('data/library.sqlite3', '.git/config', 'work/private.py',
                     'tests/test_release.py', 'AGENTS.local.md', 'requirements-dev.txt'):
            self.assertFalse(release.source_allowed(name), name)
        for name in ('server.py', 'launch.pyw', 'static/vendor/pdfjs/pdf.mjs',
                     'assets/dictionaries/ecdict.sqlite3', 'ezread/storage.py',
                     'THIRD-PARTY-NOTICES.md', 'docs/INSTALLER.md', 'docs/DERIVATION.md',
                     'scripts/migrate-library.py'):
            self.assertTrue(release.source_allowed(name), name)

    def test_distribution_record_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            site, target, licenses = root / 'site', root / 'payload', root / 'licenses'
            names = ['module.py', 'demo-1.dist-info/METADATA', 'demo-1.dist-info/RECORD',
                     'demo-1.dist-info/licenses/LICENSE', 'demo-1.dist-info/direct_url.json',
                     'demo-1.dist-info/INSTALLER', '__pycache__/module.pyc']
            for name in [*names, 'unrecorded-private.json']:
                path = site / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('test', encoding='utf-8')
            release.copy_distributions(site, [{'name': 'demo', 'version': '1',
                                       'files': [*names, '../private', 'C:/private']}], target, licenses)
            self.assertEqual({p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()},
                             {'module.py', 'demo-1.dist-info/METADATA', 'demo-1.dist-info/RECORD',
                              'demo-1.dist-info/licenses/LICENSE'})
            self.assertTrue((licenses / 'demo/demo-1.dist-info/licenses/LICENSE').is_file())
            with self.assertRaises(ValueError):
                release.copy_distributions(site, [{'name': 'demo', 'files': ['missing.py']}], target, licenses)

    def test_assets_verify_hash_and_remove_local_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'source', root / 'target'
            asset = source / 'home/.cache/babeldoc/fonts/font.ttf'
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b'public font')
            manifest = {'version': '0.6.4', 'status': 'verified', 'paths': {'home': 'C:/private'},
                        'assets': [{'path': 'fonts/font.ttf', 'size': asset.stat().st_size,
                                    'sha256': release.sha256(asset)}]}
            (source / 'runtime-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (source / 'home/private-token').write_text('private', encoding='utf-8')
            release.copy_assets(source, target)
            clean = json.loads((target / 'runtime-manifest.json').read_text(encoding='utf-8'))
            self.assertNotIn('paths', clean)
            self.assertFalse((target / 'home/private-token').exists())
            asset.write_bytes(b'wrong  font')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                release.copy_assets(source, target)

    def test_engine_support_dll_uses_only_redistributable_wheel_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            site, scripts = root / 'site', root / 'Scripts'
            libraries = site / 'sklearn/.libs'
            libraries.mkdir(parents=True)
            for name in ('msvcp140.dll', 'vcomp140.dll', 'private.dll'):
                (libraries / name).write_bytes(name.encode('ascii'))
            release.copy_engine_support_dll(site, scripts)
            self.assertEqual({path.name for path in scripts.iterdir()}, {'msvcp140.dll'})
            self.assertEqual(release.sha256(libraries / 'msvcp140.dll'),
                             release.sha256(scripts / 'msvcp140.dll'))

    def test_python_archive_hash_and_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'python.zip'
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.writestr('python.exe', 'public runtime')
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                release.extract_python(archive, root / 'runtime', '..', './Lib/site-packages')
            with patch.object(release, 'PYTHON_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest()):
                release.extract_python(archive, root / 'runtime', '..', './Lib/site-packages')
            self.assertEqual((root / 'runtime/python312._pth').read_text(encoding='utf-8'),
                             'python312.zip\n.\n./Lib/site-packages\n../\n')
            with zipfile.ZipFile(archive, 'w') as stream:
                stream.writestr('../private', 'invalid')
            with patch.object(release, 'PYTHON_SHA256', release.sha256(archive)):
                with self.assertRaises(ValueError):
                    release.extract_python(archive, root / 'unsafe', '..', './Lib/site-packages')

    @unittest.skipUnless(os.name == 'nt', 'Windows embedded Python path regression')
    def test_real_embedded_python_resolves_app_root_without_user_site(self):
        archive = release.ROOT / '.deps/packaging/python-3.12.10-embed-amd64.zip'
        if not archive.is_file():
            self.skipTest('Official offline embedded Python archive is not present')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'release_probe_module.py').write_text('VALUE = 123\n', encoding='utf-8')
            for relative, app_path, packages in (
                    ('runtime', '..', './Lib/site-packages'),
                    ('.deps/babeldoc/runtime/Scripts', '../../../..', '../Lib/site-packages')):
                runtime = root / relative
                release.extract_python(archive, runtime, app_path, packages)
                code = ('import json,sys,ssl,sqlite3,release_probe_module;'
                        'print(json.dumps(dict(paths=sys.path,no_site=sys.flags.no_site,'
                        'isolated=sys.flags.isolated,value=release_probe_module.VALUE)))')
                result = subprocess.run([str(runtime / 'python.exe'), '-B', '-c', code],
                                        cwd=release.ROOT, check=True, capture_output=True,
                                        encoding='utf-8')
                state = json.loads(result.stdout)
                self.assertEqual(state['value'], 123)
                self.assertEqual(state['no_site'], 1)
                self.assertEqual(state['isolated'], 1)
                self.assertIn(str(root), state['paths'])
                self.assertTrue(all(Path(path).is_relative_to(root) for path in state['paths']))

    def test_tracked_sources_merge_unignored_files_and_exclude_ignored_tracked(self):
        responses = [subprocess.CompletedProcess([], 0,
                     stdout=b'server.py\0ezread/new.py\0server.py\0data/private.json\0ezread/local.py\0'),
                     subprocess.CompletedProcess([], 0, stdout=b'ezread/local.py\0')]
        with patch.object(release.subprocess, 'run', side_effect=responses) as run:
            self.assertEqual(release.tracked_sources(Path('unused')), ['ezread/new.py', 'server.py'])
        self.assertEqual(run.call_args_list[0].args[0],
                         ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'])

    def test_source_materials_only_public_archives_and_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'source', root / 'target'
            source.mkdir()
            for name in ('public.tar.gz', 'public.zip', 'manifest.json', 'README.md',
                         'api-key.json', 'library.sqlite3', 'private.py', 'manifest.exe'):
                (source / name).write_text('test', encoding='utf-8')
            (source / 'incomplete.zip').touch()
            release.copy_source_materials(source, target)
            self.assertEqual({p.name for p in target.iterdir()},
                             {'public.tar.gz', 'public.zip', 'manifest.json', 'README.md'})

    def test_refresh_manifest_includes_late_launcher_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'payload'
            output.mkdir()
            (output / 'EzRead.exe').write_bytes(b'launcher')
            metadata = {'version': '1', 'packages': {'main': []}}
            self.assertEqual(release.refresh_manifest(output, metadata, root), 1)
            before = (output / 'release-manifest.json').read_bytes()
            self.assertEqual(release.refresh_manifest(output, root=root), 1)
            self.assertEqual((output / 'release-manifest.json').read_bytes(), before)
            self.assertEqual(json.loads(before)['files']['EzRead.exe']['sha256'],
                             release.sha256(output / 'EzRead.exe'))

    def test_existing_and_external_output_are_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'existing'
            output.mkdir()
            sentinel = output / 'keep.txt'
            sentinel.write_text('keep', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                release.build(output, root / 'absent', root, root, root=root)
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
            with self.assertRaises(ValueError):
                release.build(root.parent / 'outside', root / 'absent', root, root, root=root)


if __name__ == '__main__':
    unittest.main()
