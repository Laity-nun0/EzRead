"""迁移握手只使用模拟进程句柄与临时控制文件。"""
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ezread import data_migration as migration

ROOT = Path(__file__).resolve().parents[1]


class MigrationHandshakeTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='migration-handshake-', dir=ROOT / 'work')
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.options = SimpleNamespace(root=base / 'app', source=base / 'source', target=base / 'target',
                                       control_dir=base / 'control', owner_pid=123, browser_pid=124,
                                       url='http://127.0.0.1:12345', verification_session='test-session')
        self.options.control_dir.mkdir()
        self.state = {'pid': 125, 'instance_id': 'instance'}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.kernel = Mock()
        self.stack.enter_context(patch.object(migration, '_kernel', return_value=self.kernel))
        self.stack.enter_context(patch.object(migration, '_hold_process', side_effect=[1, 2, 3]))
        self.stack.enter_context(patch.object(migration, '_health', return_value=(self.state, 12345)))
        self.wait = self.stack.enter_context(patch.object(migration, '_wait_exit'))
        for name in ('location_lock', 'data_use_lock'):
            self.stack.enter_context(patch.object(migration.data_location, name, side_effect=lambda *_: nullcontext()))
        self.copy = self.stack.enter_context(patch.object(migration.data_location, 'migrate_library',
                                                         return_value={'source_preserved': True}))
        probe = self.stack.enter_context(patch.object(migration.socket, 'socket'))
        probe.return_value.__enter__.return_value.connect_ex.return_value = 1

    def approve(self, value=None):
        value = value or {'approved': True, 'owner_pid': 123}
        (self.options.control_dir / 'approved.json').write_text(json.dumps(value), encoding='utf-8')

    def test_ready_approval_all_processes_and_copy_sequence(self):
        self.approve()
        self.assertEqual(migration.migrate_after_close(self.options), {'source_preserved': True})
        self.assertEqual([call.args[1] for call in self.wait.call_args_list], [1, 3, 2])
        self.copy.assert_called_once_with(self.options.root, self.options.source, self.options.target)
        ready = json.loads((self.options.control_dir / 'ready.json').read_text())
        self.assertEqual(ready, {'ready': True, 'pid': os.getpid()})

    def test_host_crash_without_approval_never_copies(self):
        with self.assertRaises(FileNotFoundError):
            migration.migrate_after_close(self.options)
        self.copy.assert_not_called()

    def test_approval_of_another_host_is_rejected(self):
        self.approve({'approved': True, 'owner_pid': 999})
        with self.assertRaisesRegex(ValueError, '未确认'):
            migration.migrate_after_close(self.options)
        self.copy.assert_not_called()

    def test_live_backend_or_browser_never_copies(self):
        self.approve()
        for index in (1, 2):
            self.wait.side_effect = [None] * index + [ValueError('仍在运行')]
            with self.assertRaisesRegex(ValueError, '仍在运行'):
                migration.migrate_after_close(self.options)
            self.copy.assert_not_called()
            # Each attempt opens a new set of stable handles.
            migration._hold_process.side_effect = [1, 2, 3]

    def test_invalid_backend_health_stops_before_ready(self):
        migration._health.side_effect = [(self.state, 12345), ({'pid': 126, 'instance_id': 'new'}, 12345)]
        with self.assertRaisesRegex(ValueError, '已变化'):
            migration.migrate_after_close(self.options)
        self.assertFalse((self.options.control_dir / 'ready.json').exists())
        self.copy.assert_not_called()

    def test_wait_timeout_does_not_treat_process_as_exited(self):
        self.kernel.WaitForSingleObject.return_value = 258
        real_wait = self.original_wait
        with self.assertRaisesRegex(ValueError, '尚未退出'):
            real_wait(self.kernel, 123, .01, '测试进程')

    def test_restart_original_does_not_change_parent_environment(self):
        with patch.object(migration.subprocess, 'Popen') as spawn:
            before = os.environ.get('EZREAD_DATA_DIR')
            migration._restart(self.options, original=True)
            self.assertEqual(spawn.call_args.kwargs['env']['EZREAD_DATA_DIR'], str(self.options.source))
            self.assertEqual(os.environ.get('EZREAD_DATA_DIR'), before)


MigrationHandshakeTests.original_wait = staticmethod(migration._wait_exit)

if __name__ == '__main__':
    unittest.main()
