"""清库只针对已关闭的指定库，先备份，可回滚，凭据从不进入备份。"""
from contextlib import closing, nullcontext
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from browser_state import prepare_migration
from ezread.storage import init_db

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('reset_library', ROOT / 'scripts/reset-library.py')
maintenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maintenance)


class LibraryResetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'work', prefix='reset-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        init_db(SimpleNamespace(DATA=self.data, LIBRARY=self.data / 'library', all_docs=lambda **kwargs: []))
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as connection, connection:
            connection.execute('INSERT INTO papers VALUES(?,?,?,?)', ('fixture', 'hash', '{}', 0))
            connection.execute('INSERT INTO settings VALUES(?,?)', ('collection', '["old"]'))
        for name in maintenance.DIRECTORIES:
            path = self.data / name
            path.mkdir(exist_ok=True)
            (path / 'old-state').write_bytes(b'old fixture')
        self.key = self.data / 'translation-api.private.json'
        self.key.write_bytes(b'{"fixture": "configuration to preserve"}')
        self.catalogue = self.data / 'journal_tiers.csv'
        self.catalogue.write_bytes(b'fixture catalogue')
        for name in ('webview2-migration.json', 'desktop-close-drafts.json'):
            (self.data / name).write_text('{"old-draft":"fixture"}', encoding='utf-8')
        self.backup = self.root / 'backup'
        self.addCleanup(patch.stopall)
        patch.object(maintenance, 'startup_lock', return_value=nullcontext()).start()
        patch.object(maintenance, 'ensure_offline').start()

    def test_reset_clears_all_rows_resources_profiles_and_drafts_without_copying_api_configuration(self):
        before_api, before_catalogue = self.key.read_bytes(), self.catalogue.read_bytes()
        report = maintenance.reset(self.data, self.backup)
        self.assertTrue(report['api_configuration_unchanged'])
        self.assertFalse(any(maintenance.counts(self.data).values()))
        self.assertEqual(list((self.data / 'library').iterdir()), [])
        self.assertEqual(list((self.data / 'tmp').iterdir()), [])
        for name in ('clipboard', 'webview2-profile', 'browser-profile', 'desktop-close-drafts.json'):
            self.assertFalse((self.data / name).exists())
        prepare_migration(self.data, 'http://127.0.0.1:47831')
        self.assertEqual(json.loads((self.data / 'webview2-migration.json').read_text()), {})
        self.assertEqual(self.key.read_bytes(), before_api)
        self.assertEqual(self.catalogue.read_bytes(), before_catalogue)
        with closing(sqlite3.connect(self.backup / 'library.sqlite3')) as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0], 1)
        with zipfile.ZipFile(self.backup / 'resources.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()), {'library/old-state', 'clipboard/old-state'})
        self.assertEqual({path.name for path in self.backup.iterdir()},
                         {'library.sqlite3', 'resources.zip', 'report.json'})

    def test_install_failure_restores_database_resources_and_profiles(self):
        rename = Path.rename

        def fail_install(path, destination):
            if path.parent.name == 'empty' and path.name == 'library.sqlite3':
                raise OSError('模拟安装失败')
            return rename(path, destination)

        with patch.object(Path, 'rename', fail_install), self.assertRaises(OSError):
            maintenance.reset(self.data, self.backup)
        self.assertEqual(maintenance.counts(self.data)['papers'], 1)
        for name in maintenance.DIRECTORIES:
            self.assertEqual((self.data / name / 'old-state').read_bytes(), b'old fixture')
        self.assertEqual(json.loads((self.data / 'webview2-migration.json').read_text()), {'old-draft': 'fixture'})

    def test_explicit_engine_cache_reset_preserves_public_assets(self):
        base = self.root / '.deps' / 'babeldoc'
        cache = base / 'home' / '.cache' / 'babeldoc'
        (cache / 'fonts').mkdir(parents=True)
        (cache / 'fonts' / 'public-font').write_bytes(b'public font')
        (cache / 'cache.v1.db').write_bytes(b'old translation cache')
        (base / 'tmp').mkdir()
        (base / 'tmp' / 'old').write_bytes(b'temporary content')
        with patch.object(maintenance, 'ROOT', self.root):
            maintenance.reset(self.data, self.backup, engine_cache=True)
        self.assertFalse((cache / 'cache.v1.db').exists())
        self.assertFalse((base / 'tmp').exists())
        self.assertEqual((cache / 'fonts' / 'public-font').read_bytes(), b'public font')

    def test_backup_failure_leaves_active_library_untouched(self):
        with patch.object(maintenance, 'snapshot', side_effect=OSError('模拟备份失败')), self.assertRaises(OSError):
            maintenance.reset(self.data, self.backup)
        self.assertEqual(maintenance.counts(self.data)['papers'], 1)
        self.assertTrue((self.data / 'library' / 'old-state').exists())

    def test_post_install_verification_failure_restores_original_library(self):
        with patch.object(maintenance, 'api_digest', side_effect=['before', 'changed']):
            with self.assertRaises(RuntimeError):
                maintenance.reset(self.data, self.backup)
        self.assertEqual(maintenance.counts(self.data)['papers'], 1)
        self.assertEqual((self.data / 'library' / 'old-state').read_bytes(), b'old fixture')

    def test_running_service_refuses_before_backup(self):
        with patch.object(maintenance, 'ensure_offline', side_effect=RuntimeError('请先关闭')), self.assertRaises(RuntimeError):
            maintenance.reset(self.data, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual(maintenance.counts(self.data)['papers'], 1)

    def test_link_cannot_escape_into_adjacent_directory(self):
        adjacent = self.root / 'adjacent'
        adjacent.mkdir()
        marker = adjacent / 'keep'
        marker.write_bytes(b'keep')
        link = self.data / 'library' / 'unsafe-link'
        try:
            link.symlink_to(adjacent, target_is_directory=True)
        except OSError:
            self.skipTest('当前 Windows 不允许创建符号链接')
        with self.assertRaises(ValueError):
            maintenance.reset(self.data, self.backup)
        self.assertEqual(marker.read_bytes(), b'keep')

    def test_overlapping_backup_directory_is_refused(self):
        with self.assertRaises(ValueError):
            maintenance.reset(self.data, self.data / 'backup')
        self.assertEqual(maintenance.counts(self.data)['papers'], 1)

    def test_reparse_guard_refuses_without_needing_symlink_privilege(self):
        target = self.data / 'library'
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == target or original(path)):
            with self.assertRaises(ValueError):
                maintenance.reset(self.data, self.backup)
        self.assertEqual(maintenance.counts(self.data)['papers'], 1)
        self.assertFalse(self.backup.exists())


class OfflineProbeTests(unittest.TestCase):
    def test_timeout_is_not_treated_as_closed_service(self):
        with patch.object(maintenance.urllib.request, 'urlopen',
                          side_effect=urllib.error.URLError(TimeoutError())), self.assertRaises(RuntimeError):
            maintenance.ensure_offline(ROOT / 'work' / 'unused')

    def test_only_connection_refused_proves_port_is_closed(self):
        with patch.object(maintenance.urllib.request, 'urlopen',
                          side_effect=urllib.error.URLError(ConnectionRefusedError())):
            maintenance.ensure_offline(ROOT / 'work' / 'unused')
