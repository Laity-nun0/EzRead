"""A layout correction must not discard or reassign saved reading data."""
import copy
from contextlib import closing, nullcontext
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ezread.extraction import repair_document
from ezread.text_actions import original_text, model_document, translatable_blocks

_spec = importlib.util.spec_from_file_location('repair_extraction_cli', Path(__file__).resolve().parents[1] / 'scripts/repair-extraction.py')
maintenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maintenance)


def block(bid, text, box, **fields):
    return {'id': bid, 'text': text, 'bbox': box, 'page': 1, 'kind': 'paragraph', **fields}


class ExtractionRepairTests(unittest.TestCase):
    def setUp(self):
        self.original = {'id': 'a' * 16, 'title': 'Synthetic fixture', 'notes': 'saved notes',
            'collection': 'manual collection', 'reader_state': {'translation_block_id': 'body'},
            'translation': {'status': 'completed', 'config': {'model': 'mock'}},
            'blocks': [block('body', 'Measured at 300 C.', [.1, .2, .4, .23], translation='在300摄氏度测量。'),
                       block('unit', '[公式：请对照原文图像]', [.28, .19, .29, .21], is_formula=True)],
            'figures': [], 'translation_versions': [{'id': 'version', 'blocks': {'body': {'translation': 'old'}}}]}
        self.extracted = {'blocks': [block('new-id', 'Measured at 300 °C.', [.1, .19, .4, .23])],
                          'figures': [], 'extraction': {'version': 5}}

    def test_original_ids_text_translations_and_metadata_are_retained(self):
        before = copy.deepcopy(self.original)
        repaired, report = repair_document(self.original, self.extracted, 'now')
        self.assertEqual(self.original, before)
        by_id = {b['id']: b for b in repaired['blocks']}
        for old in before['blocks']:
            for field in ('id', 'text', 'translation'):
                self.assertEqual(by_id[old['id']].get(field), old.get(field))
        for field in ('notes', 'collection', 'reader_state', 'translation_versions'):
            self.assertEqual(repaired[field], before[field])
        self.assertEqual(original_text(by_id['body']), 'Measured at 300 °C.')
        self.assertTrue(by_id['unit']['extraction_hidden'])
        self.assertEqual(report['hidden'], ['unit'])
        self.assertEqual([b['id'] for b in model_document(repaired)['blocks']], ['body'])

    def test_user_override_and_annotations_are_not_merged_or_overwritten(self):
        self.original['blocks'][1]['note'] = 'saved unit note'
        self.original['blocks'][0]['text_override'] = 'User corrected sentence.'
        self.original['text_annotations'] = [{'id': 'mark', 'ranges': [{'block_id': 'body', 'start': 0, 'end': 4}]}]
        repaired, report = repair_document(self.original, self.extracted, 'now')
        self.assertTrue(report['warnings'])
        self.assertFalse(any(b.get('extraction_hidden') for b in repaired['blocks']))
        self.assertEqual(repaired['text_annotations'], self.original['text_annotations'])
        by_id = {b['id']: b for b in repaired['blocks']}
        self.assertEqual(original_text(by_id['body']), 'User corrected sentence.')
        self.assertEqual(by_id['unit']['note'], 'saved unit note')

    def test_unchanged_parent_does_not_consume_overlapping_independent_blocks(self):
        self.original['blocks'][0]['text'] = 'Measured at 300 °C.'
        self.original['blocks'][1].update(text='1', is_formula=False, translation='1')
        self.extracted['blocks'].append(block('new-unit', '1', [.28, .19, .29, .21]))
        repaired, report = repair_document(self.original, self.extracted, 'now')
        self.assertFalse(report['added'])
        self.assertEqual({b['id'] for b in repaired['blocks']}, {'body', 'unit'})
        self.assertFalse(any(b.get('extraction_hidden') for b in repaired['blocks']))

    def test_missing_caption_is_added_without_replacing_completed_translations(self):
        self.extracted['blocks'].append(block('new-caption', 'Fig. 2. A recovered caption.', [.1, .8, .9, .9], kind='caption'))
        repaired, report = repair_document(self.original, self.extracted, 'now')
        self.assertEqual(len(report['added']), 1)
        self.assertEqual(repaired['translation']['status'], 'paused')
        self.assertEqual(repaired['translation']['config'], {'model': 'mock'})
        self.assertEqual(next(b for b in repaired['blocks'] if b['id'] == 'body')['translation'], '在300摄氏度测量。')

    def test_formula_number_already_in_expanded_source_image_is_retained_hidden(self):
        self.original['blocks'][0].update(text='[公式：请对照原文图像]', is_formula=True)
        self.extracted['blocks'][0].update(text='[公式：请对照原文图像]', is_formula=True)
        repaired, report = repair_document(self.original, self.extracted, 'now')
        number = next(b for b in repaired['blocks'] if b['id'] == 'unit')
        self.assertTrue(number['extraction_hidden'])
        self.assertEqual(number['extraction_parent_id'], 'body')
        self.assertFalse(report['warnings'])

    def test_replaced_interleaved_columns_are_hidden_without_losing_sources(self):
        left = 'The left column states measured hardening.'
        right = 'The right column reports fatigue damage.'
        self.original['blocks'] = [block('left', left, [.1, .15, .48, .35]),
                                   block('right', right, [.52, .15, .9, .35]),
                                   block('mixed', left + ' ' + right, [.1, .2, .9, .225])]
        self.extracted['blocks'] = [block('new-left', left, [.1, .15, .48, .35]),
                                    block('new-right', right, [.52, .15, .9, .35])]
        repaired, report = repair_document(self.original, self.extracted, 'now')
        mixed = next(b for b in repaired['blocks'] if b['id'] == 'mixed')
        self.assertTrue(mixed['extraction_hidden'])
        self.assertEqual(mixed['text'], left + ' ' + right)
        self.assertFalse(report['warnings'])
        for fields in ({'note': 'Saved note'}, {'translation': 'Saved translation'}):
            with self.subTest(fields=fields):
                self.original['blocks'][2].update(fields)
                protected, warnings = repair_document(self.original, self.extracted, 'now')
                saved = next(b for b in protected['blocks'] if b['id'] == 'mixed')
                self.assertFalse(saved.get('extraction_hidden'))
                self.assertTrue(warnings['warnings'])
                for name in fields:
                    self.original['blocks'][2].pop(name)
        left = 'Fatigue cracks reveal measured hardening.'
        self.original['blocks'][0]['text'] = self.extracted['blocks'][0]['text'] = left
        self.original['blocks'][2]['text'] = 'gue cracks reveal measured hardening. ' + right
        repaired, report = repair_document(self.original, self.extracted, 'now')
        self.assertTrue(next(b for b in repaired['blocks'] if b['id'] == 'mixed')['extraction_hidden'])
        self.assertFalse(report['warnings'])

    def test_explicit_empty_override_remains_empty(self):
        self.assertEqual(original_text({'text': 'old', 'extraction_text': 'recovered', 'text_override': ''}), '')

    def test_table_cells_are_not_translation_targets_or_model_prose(self):
        self.original['blocks'].extend([
            block('caption', 'Table 2. Test results.', [.1, .4, .9, .43], kind='caption'),
            block('table', 'CELL_MARKER | 430_06 | 400', [.1, .45, .9, .8], is_table=True, translation='old cells')])
        before = copy.deepcopy(self.original)
        self.assertEqual([b['id'] for b in translatable_blocks(self.original)], ['body', 'caption'])
        self.assertNotIn('CELL_MARKER', json.dumps(model_document(self.original)['blocks']))
        self.assertEqual(self.original, before)

    def test_partially_overlapping_user_revision_keeps_its_layout(self):
        old = self.original['blocks'][0]
        old['text_override'] = 'Measured at 300 °C, verified.'
        self.extracted['blocks'][0]['bbox'] = [.2, .2, .4, .23]
        repaired, report = repair_document(self.original, self.extracted, 'now')
        current = next(b for b in repaired['blocks'] if b['id'] == 'body')
        self.assertEqual(current['bbox'], old['bbox'])
        self.assertEqual(current['text_override'], old['text_override'])
        self.assertTrue(report['warnings'])

    def test_hidden_panel_points_to_retained_figure_id(self):
        self.original['blocks'] = [block('panel', 'a', [.2, .3, .22, .32])]
        self.original['figures'] = [{'id': 'saved-figure', 'page': 1, 'bbox': [.1, .2, .9, .8], 'caption': 'Fig. 2. Panels.'}]
        self.extracted['blocks'] = []
        self.extracted['figures'] = [{'id': 'new-figure', 'page': 1, 'bbox': [.1, .2, .9, .8], 'caption': 'Fig. 2. Panels.'}]
        repaired, _ = repair_document(self.original, self.extracted, 'now')
        self.assertEqual(repaired['figures'][0]['id'], 'saved-figure')
        self.assertEqual(repaired['blocks'][0]['extraction_parent_id'], 'saved-figure')


class ExtractionCommitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.data, self.stage, self.snapshot = root / 'data', root / 'stage', root / 'backup'
        self.pid = 'a' * 16
        self.assets = self.data / 'library' / self.pid
        self.assets.mkdir(parents=True)
        (self.assets / 'original.pdf').write_bytes(b'unchanged source')
        (self.stage / self.pid).mkdir(parents=True)
        (self.stage / self.pid / 'new.jpg').write_bytes(b'repaired crop')
        self.before = json.dumps({'notes': 'saved notes', 'revision': 1})
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as connection, connection:
            connection.execute('CREATE TABLE papers (id TEXT PRIMARY KEY, doc TEXT)')
            connection.execute('INSERT INTO papers VALUES (?, ?)', (self.pid, self.before))
        self.plans = {self.pid: {'doc': {'notes': 'saved notes', 'revision': 2}, 'files': {'new.jpg': 'layout-new.jpg'}}}
        self.addCleanup(patch.stopall)
        patch.object(maintenance, 'startup_lock', return_value=nullcontext()).start()
        self.offline = patch.object(maintenance, 'ensure_offline').start()

    def commit(self):
        maintenance.commit(self.data, [self.pid], self.plans, {self.pid: self.before}, self.stage, self.snapshot)

    def test_commit_verifies_backup_before_adding_resources_and_document(self):
        self.commit()
        with closing(sqlite3.connect(self.snapshot / 'library.sqlite3')) as backup:
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(backup.execute('SELECT doc FROM papers').fetchone()[0], self.before)
        with zipfile.ZipFile(self.snapshot / 'resources.zip') as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read(f'{self.pid}/original.pdf'), b'unchanged source')
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as connection:
            self.assertEqual(json.loads(connection.execute('SELECT doc FROM papers').fetchone()[0]), self.plans[self.pid]['doc'])
        self.assertEqual((self.assets / 'original.pdf').read_bytes(), b'unchanged source')
        self.assertEqual((self.assets / 'layout-new.jpg').read_bytes(), b'repaired crop')

    def test_concurrent_document_change_is_not_overwritten(self):
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as connection, connection:
            connection.execute('UPDATE papers SET doc=?', ('concurrent edit',))
        with self.assertRaisesRegex(RuntimeError, '文献发生变化'):
            self.commit()
        with closing(sqlite3.connect(self.data / 'library.sqlite3')) as connection:
            self.assertEqual(connection.execute('SELECT doc FROM papers').fetchone()[0], 'concurrent edit')
        self.assertFalse((self.assets / 'layout-new.jpg').exists())

    def test_active_application_prevents_backup_and_commit(self):
        self.offline.side_effect = RuntimeError('应用仍在运行')
        with self.assertRaisesRegex(RuntimeError, '应用仍在运行'):
            self.commit()
        self.assertFalse(self.snapshot.exists())
        self.assertFalse((self.assets / 'layout-new.jpg').exists())


if __name__ == '__main__':
    unittest.main()
