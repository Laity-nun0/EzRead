"""Renamed configuration must keep the existing library and CLI selection."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_bridge
from ezread.config import data_directory, environment_value


class ConfigurationTests(unittest.TestCase):
    def test_default_and_relative_paths_belong_to_the_checkout(self):
        self.assertEqual(data_directory(ROOT, {}), ROOT / 'data')
        self.assertEqual(data_directory(ROOT, {'EZREAD_DATA_DIR': 'work/isolated'}), ROOT / 'work' / 'isolated')

    def test_new_directory_wins_and_explicit_empty_does_not_restore_old_path(self):
        values = {'EZREAD_DATA_DIR': 'work/new', 'TUDU_DATA_DIR': 'work/previous'}
        self.assertEqual(data_directory(ROOT, values), ROOT / 'work' / 'new')
        values['EZREAD_DATA_DIR'] = ''
        self.assertEqual(data_directory(ROOT, values), ROOT / 'data')

    def test_old_directory_still_resolves_to_the_existing_library(self):
        self.assertEqual(data_directory(ROOT, {'TUDU_DATA_DIR': 'work/existing'}), ROOT / 'work' / 'existing')

    def test_saved_location_wins_over_installer_default_but_not_explicit_override(self):
        chosen = ROOT / 'work' / 'chosen-library'
        with patch('ezread.data_location.saved_directory', return_value=chosen) as saved:
            self.assertEqual(data_directory(ROOT, {'EZREAD_DEFAULT_DATA_DIR': str(ROOT / 'work/default')}), chosen)
            saved.assert_called_once()
            saved.reset_mock()
            self.assertEqual(data_directory(ROOT, {'EZREAD_DATA_DIR': 'work/explicit'}), ROOT / 'work/explicit')
            saved.assert_not_called()
        with patch('ezread.data_location.saved_directory', return_value=None):
            self.assertEqual(data_directory(ROOT, {'EZREAD_DEFAULT_DATA_DIR': str(chosen)}), chosen)

    def test_model_and_executable_aliases_use_the_same_precedence(self):
        for kind in ('CODEX_PATH', 'CODEX_MODEL'):
            new, previous = 'EZREAD_' + kind, 'TUDU_' + kind
            self.assertEqual(environment_value(new, environment={previous: 'existing'}), 'existing')
            self.assertEqual(environment_value(new, environment={new: 'chosen', previous: 'existing'}), 'chosen')
            self.assertEqual(environment_value(new, environment={new: '', previous: 'existing'}), '')

    def test_bridge_selects_the_new_explicit_cli_without_running_it(self):
        with tempfile.TemporaryDirectory() as temp:
            chosen, previous = Path(temp) / 'chosen.exe', Path(temp) / 'previous.exe'
            chosen.write_bytes(b'not an executable')
            previous.write_bytes(b'not an executable')
            with patch.dict(os.environ, {'EZREAD_CODEX_PATH': str(chosen), 'TUDU_CODEX_PATH': str(previous)}):
                self.assertEqual(codex_bridge._cli(), str(chosen.resolve()))

    def test_bridge_prefers_native_bundle_over_path_wrapper(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp)
            native = local / 'OpenAI' / 'Codex' / 'bin' / 'build' / 'codex.exe'
            native.parent.mkdir(parents=True)
            native.write_bytes(b'not an executable')
            wrapper = str(local / 'npm' / 'codex')
            with patch.dict(os.environ, {'LOCALAPPDATA': temp, 'EZREAD_CODEX_PATH': ''}), \
                 patch.object(codex_bridge.shutil, 'which', side_effect=lambda name: wrapper if name == 'codex' else None):
                self.assertEqual(codex_bridge._cli(), str(native))

    def test_bridge_keeps_native_path_priority(self):
        native = str(ROOT / 'work' / 'path-codex.exe')
        with patch.dict(os.environ, {'EZREAD_CODEX_PATH': ''}), \
             patch.object(codex_bridge.shutil, 'which', side_effect=lambda name: native if name == 'codex.exe' else None):
            self.assertEqual(codex_bridge._cli(), native)


if __name__ == '__main__':
    unittest.main()
