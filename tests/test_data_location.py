"""目录迁移只使用 work 下独立合成文献库。"""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ezread import data_location as locations


class DataLocationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'work').mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'work', prefix='data-location-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / '程序 中文'
        self.root.mkdir()
        self.source = self.root / 'data'
        self.source.mkdir()
        self.target = self.base / '文献 空格' / '目标目录'
        self.environment = {'LOCALAPPDATA': str(self.base / '本机 配置')}
        with closing(sqlite3.connect(self.source / 'library.sqlite3')) as connection:
            connection.executescript('CREATE TABLE papers(id TEXT PRIMARY KEY, hash TEXT, doc TEXT, deleted INTEGER);'
                                     'CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);')
        self.resources = {'library/论文/source.pdf': b'%PDF-synthetic\x00\xff',
                          'library/论文/translation.pdf': b'%PDF-translated',
                          'desktop-close-drafts.json': '{"draft":"草稿"}'.encode(),
                          'api-private.json': b'opaque-private-fixture\x00\x01',
                          'profile/Local Storage/cache': b'profile-fixture',
                          'tmp/nested/library.sqlite3-wal': b'non-root-wal-resource'}
        for relative, content in self.resources.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.source / 'empty-folder').mkdir()

    def migrate(self):
        with locations.location_lock(self.root), locations.data_use_lock(self.source), locations.data_use_lock(self.target):
            return locations.migrate_library(self.root, self.source, self.target, self.environment)

    def test_wal_backup_and_all_resources_are_preserved(self):
        connection = sqlite3.connect(self.source / 'library.sqlite3')
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('INSERT INTO papers VALUES(?,?,?,?)', ('p1', 'hash', '{"translation":"译文"}', 0))
        connection.commit()
        self.assertGreater((self.source / 'library.sqlite3-wal').stat().st_size, 0)
        result = self.migrate()
        self.assertTrue(result['source_preserved'])
        self.assertEqual(result['copied_files'], len(self.resources) + 1)
        self.assertGreater(result['bytes'], 0)
        self.assertEqual(locations.saved_directory(self.root, self.environment), self.target)
        with closing(sqlite3.connect(self.target / 'library.sqlite3')) as backup:
            self.assertEqual(backup.execute('SELECT id,doc FROM papers').fetchone(), ('p1', '{"translation":"译文"}'))
        for relative, content in self.resources.items():
            self.assertEqual((self.source / relative).read_bytes(), content)
            self.assertEqual((self.target / relative).read_bytes(), content)
        self.assertTrue((self.target / 'empty-folder').is_dir())
        self.assertFalse((self.target / 'library.sqlite3-wal').exists())
        self.assertTrue((self.source / 'library.sqlite3').is_file())

    def test_configuration_is_per_root_and_missing_is_not_created(self):
        self.assertIsNone(locations.saved_directory(self.root, self.environment))
        config = locations.location_file(self.root, self.environment)
        self.assertFalse(config.exists())
        self.assertFalse(config.is_relative_to(self.root))
        self.assertNotEqual(config, locations.location_file(self.base / 'another', self.environment))
        locations.save_directory(self.root, self.source, self.environment)
        self.assertEqual(locations.saved_directory(self.root, self.environment), self.source)
        self.assertEqual(set(json.loads(config.read_text(encoding='utf-8'))), {'version', 'app_root', 'data_dir'})

    def test_saved_directory_checks_identity_without_full_database_scan(self):
        locations.save_directory(self.root, self.source, self.environment)
        connection = sqlite3.connect(self.source / 'library.sqlite3')
        queries = []
        connection.set_trace_callback(queries.append)
        with patch.object(locations, '_open_database', return_value=connection):
            self.assertEqual(locations.saved_directory(self.root, self.environment), self.source)
        self.assertFalse(any('quick_check' in query.lower() for query in queries))
        self.assertTrue(any('sqlite_master' in query for query in queries))

    def test_missing_invalid_or_mismatched_saved_library_never_falls_back(self):
        config = locations.location_file(self.root, self.environment)
        config.parent.mkdir(parents=True)
        for content in ('bad-json', '[]', '{"version":1,"app_root":"other"}'):
            config.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                locations.saved_directory(self.root, self.environment)
        locations.save_directory(self.root, self.base / 'missing', self.environment)
        with self.assertRaisesRegex(ValueError, '缺失'):
            locations.saved_directory(self.root, self.environment)
        locations.save_directory(self.root, self.source, self.environment)
        (self.source / 'library.sqlite3').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            locations.saved_directory(self.root, self.environment)

    def test_nonempty_and_overlapping_targets_are_rejected(self):
        self.target.mkdir(parents=True)
        (self.target / 'keep').write_bytes(b'keep')
        for target in (self.target, self.source, self.source / 'child', self.root, self.root / 'sibling', self.base):
            with self.subTest(target=target), self.assertRaises(ValueError):
                locations.migrate_library(self.root, self.source, target, self.environment)
        self.assertEqual((self.target / 'keep').read_bytes(), b'keep')

    def test_relative_paths_and_program_ancestors_are_rejected(self):
        for source, target in ((Path('relative'), self.target), (self.source, Path('relative')),
                               (self.source, self.base)):
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                locations.migrate_library(self.root, source, target, self.environment)

    def test_empty_existing_target_is_allowed(self):
        self.target.mkdir(parents=True)
        self.migrate()
        self.assertTrue((self.target / 'library.sqlite3').is_file())

    def test_source_mutation_does_not_publish_or_switch(self):
        copy = shutil.copyfile

        def changing_copy(source, destination):
            result = copy(source, destination)
            (self.source / 'new-file').write_bytes(b'changed')
            return result

        with patch.object(locations.shutil, 'copyfile', side_effect=changing_copy):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                locations.migrate_library(self.root, self.source, self.target, self.environment)
        self.assertFalse(self.target.exists())
        self.assertIsNone(locations.saved_directory(self.root, self.environment))
        self.assertTrue((self.source / 'library.sqlite3').is_file())

    def test_changed_file_and_corrupt_copy_are_rejected(self):
        copy = shutil.copyfile

        def corrupt_copy(source, destination):
            result = copy(source, destination)
            Path(destination).write_bytes(b'corrupt')
            return result

        with patch.object(locations.shutil, 'copyfile', side_effect=corrupt_copy):
            with self.assertRaisesRegex(ValueError, '副本核验失败'):
                self.migrate()

        def changing_copy(source, destination):
            result = copy(source, destination)
            (self.source / 'desktop-close-drafts.json').write_bytes(b'changed')
            return result

        with patch.object(locations.shutil, 'copyfile', side_effect=changing_copy):
            with self.assertRaises(ValueError):
                self.migrate()
        self.assertFalse(self.target.exists())
        self.assertIsNone(locations.saved_directory(self.root, self.environment))

    def test_target_populated_during_copy_is_never_overwritten(self):
        copy = shutil.copyfile

        def racing_copy(source, destination):
            self.target.mkdir(parents=True, exist_ok=True)
            (self.target / 'keep').write_bytes(b'keep')
            return copy(source, destination)

        with patch.object(locations.shutil, 'copyfile', side_effect=racing_copy):
            with self.assertRaisesRegex(ValueError, '目标目录已发生变化'):
                self.migrate()
        self.assertEqual((self.target / 'keep').read_bytes(), b'keep')
        self.assertIsNone(locations.saved_directory(self.root, self.environment))

    def test_unreadable_inventory_does_not_skip_resources(self):
        def inaccessible_walk(*args, **kwargs):
            kwargs['onerror'](PermissionError('fixture'))
            return iter(())

        with patch.object(locations.os, 'walk', side_effect=inaccessible_walk):
            with self.assertRaises(ValueError):
                self.migrate()
        self.assertFalse(self.target.exists())

    def test_fsync_failure_does_not_replace_old_selection(self):
        locations.save_directory(self.root, self.source, self.environment)
        config = locations.location_file(self.root, self.environment)
        previous = config.read_bytes()
        with patch.object(locations.os, 'fsync', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                locations.save_directory(self.root, self.target, self.environment)
        self.assertEqual(config.read_bytes(), previous)

    def test_corrupt_or_wrong_schema_database_cannot_switch(self):
        with closing(sqlite3.connect(self.source / 'library.sqlite3')) as connection:
            connection.execute('DROP TABLE settings')
        with self.assertRaisesRegex(ValueError, '有效'):
            self.migrate()
        (self.source / 'library.sqlite3').write_bytes(b'broken-db')
        with self.assertRaises(ValueError):
            self.migrate()
        self.assertFalse(self.target.exists())
        self.assertIsNone(locations.saved_directory(self.root, self.environment))

    def test_configuration_failure_preserves_old_choice_and_complete_copy(self):
        locations.save_directory(self.root, self.source, self.environment)
        config = locations.location_file(self.root, self.environment)
        previous = config.read_bytes()
        with patch.object(locations.os, 'replace', side_effect=PermissionError('fixture')):
            with self.assertRaises(ValueError):
                self.migrate()
        self.assertEqual(config.read_bytes(), previous)
        self.assertEqual(locations.saved_directory(self.root, self.environment), self.source)
        self.assertTrue((self.target / 'library.sqlite3').is_file())
        for relative, content in self.resources.items():
            self.assertEqual((self.target / relative).read_bytes(), content)
            self.assertEqual((self.source / relative).read_bytes(), content)

    def test_space_and_copy_permissions_fail_without_switch(self):
        with patch.object(locations.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)):
            with self.assertRaisesRegex(ValueError, '空间不足'):
                self.migrate()
        with patch.object(locations.shutil, 'copyfile', side_effect=PermissionError('fixture')):
            with self.assertRaises(ValueError):
                self.migrate()
        self.assertFalse(self.target.exists())
        self.assertIsNone(locations.saved_directory(self.root, self.environment))

    def test_acl_failure_or_unsupported_volume_never_copies_or_switches(self):
        with patch.object(locations, '_restrict_directory', side_effect=ValueError('ACL fixture')):
            with self.assertRaisesRegex(ValueError, 'ACL fixture'):
                self.migrate()
        with patch.object(locations, '_kernel') as kernel:
            kernel.return_value.GetVolumePathNameW.return_value = 0
            with self.assertRaisesRegex(ValueError, '权限保护'):
                locations.migrate_library(self.root, self.source, self.target, self.environment)
        identity = subprocess.CompletedProcess([], 0, b'"fixture","S-1-5-21-1-2-3-1000"\r\n', b'')
        rejected = subprocess.CompletedProcess([], 1, b'', b'fixture')
        accepted = subprocess.CompletedProcess([], 0, b'', b'')
        with patch.object(locations.subprocess, 'run', side_effect=[identity, rejected]):
            with self.assertRaisesRegex(ValueError, '重置.*文件权限'):
                self.migrate()
        with patch.object(locations.subprocess, 'run', side_effect=[identity, accepted, rejected]):
            with self.assertRaisesRegex(ValueError, '文件权限'):
                self.migrate()
        self.assertFalse(self.target.exists())
        self.assertIsNone(locations.saved_directory(self.root, self.environment))
        self.assertTrue((self.source / 'api-private.json').is_file())

    @unittest.skipUnless(os.name == 'nt', 'Windows NTFS ACL')
    def test_target_acl_is_current_user_only_and_inherited_by_private_files(self):
        self.migrate()
        self.assert_private_acl(self.target)

    @unittest.skipUnless(os.name == 'nt', 'Windows NTFS 显式 ACL')
    def test_explicit_system_acl_is_removed_before_private_files_are_copied(self):
        directory = self.base / '显式 权限'
        directory.mkdir()
        system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
        result = subprocess.run([str(system / 'icacls.exe'), str(directory), '/grant', '*S-1-5-18:(OI)(CI)F'],
                                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        locations._restrict_directory(directory)
        (directory / 'api-private.json').write_bytes(b'opaque-fixture')
        nested = directory / 'profile' / 'Local Storage'
        nested.mkdir(parents=True)
        (nested / 'cache').write_bytes(b'profile-fixture')
        self.assert_private_acl(directory)

    def assert_private_acl(self, directory):
        script = ('$me=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; '
                  '$paths=@($env:EZREAD_TEST_ACL_PATH,($env:EZREAD_TEST_ACL_PATH+"/api-private.json"),'
                  '($env:EZREAD_TEST_ACL_PATH+"/profile/Local Storage/cache")); '
                  '@($paths | ForEach-Object {$acl=if ([IO.Directory]::Exists($_)) {'
                  '[IO.Directory]::GetAccessControl($_)} else {[IO.File]::GetAccessControl($_)}; '
                  '[pscustomobject]@{protected=$acl.AreAccessRulesProtected; '
                  'rules=@($acl.Access | ForEach-Object {[pscustomobject]@{'
                  'sid=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value; '
                  'inherited=$_.IsInherited}}); me=$me}}) | ConvertTo-Json -Depth 4 -Compress')
        result = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True,
                                env={**os.environ, 'EZREAD_TEST_ACL_PATH': str(directory)},
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        entries = json.loads(result.stdout)
        self.assertTrue(entries[0]['protected'])
        for index, entry in enumerate(entries):
            self.assertTrue(entry['rules'])
            self.assertTrue({rule['sid'] for rule in entry['rules']} == {entry['me']})
            if index:
                self.assertTrue(all(rule['inherited'] for rule in entry['rules']))

    @unittest.skipUnless(os.name == 'nt', 'Windows 重解析点')
    def test_junction_and_junction_ancestor_are_rejected(self):
        outside = self.base / 'outside'
        outside.mkdir()
        junction = self.source / 'linked'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        try:
            with self.assertRaisesRegex(ValueError, '重解析点'):
                self.migrate()
            with self.assertRaisesRegex(ValueError, '重解析点'):
                locations.migrate_library(self.root, self.source, junction / 'target', self.environment)
            with self.assertRaisesRegex(ValueError, '重解析点'):
                locations.location_file(self.root, {'LOCALAPPDATA': str(junction)})
        finally:
            junction.rmdir()

    @unittest.skipUnless(os.name == 'nt', 'Windows 命名互斥')
    def test_location_lock_is_cross_process_and_per_root(self):
        script = ('import sys; from pathlib import Path; from ezread.data_location import location_lock; '
                  '\nwith location_lock(Path(sys.argv[1]), timeout=0.05): pass')
        with locations.location_lock(self.root):
            blocked = subprocess.run([sys.executable, '-c', script, str(self.root)], cwd=ROOT, capture_output=True)
            independent = subprocess.run([sys.executable, '-c', script, str(self.base / 'other-root')],
                                         cwd=ROOT, capture_output=True)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(independent.returncode, 0)
        with locations.location_lock(self.root, timeout=0.05):
            pass

    @unittest.skipUnless(os.name == 'nt', 'Windows 数据库占用互斥')
    def test_data_use_lock_blocks_other_processes_using_same_directory(self):
        script = ('import sys; from pathlib import Path; from ezread.data_location import data_use_lock; '
                  '\nwith data_use_lock(Path(sys.argv[1])): pass')
        with locations.data_use_lock(self.source):
            blocked = subprocess.run([sys.executable, '-c', script, str(self.source)], cwd=ROOT, capture_output=True)
            independent = subprocess.run([sys.executable, '-c', script, str(self.target)], cwd=ROOT, capture_output=True)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(independent.returncode, 0)
        with locations.data_use_lock(self.source):
            pass


if __name__ == '__main__':
    unittest.main()
