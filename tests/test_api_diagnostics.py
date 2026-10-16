"""模拟外层失败、部分传输与写盘失败；不调用真实供应商。"""
import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_bridge import TranslationError
from ezread import api_diagnostics
from ezread.translation_api import ApiTranslationSession
from tests.test_translation_api import KEY, PROMPT, mock_api, response_for


class ApiDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'api-diagnostics'

    def failure(self, reply, code, **options):
        with mock_api(reply=reply, **options) as (config, records, _, _):
            with ApiTranslationSession(config, KEY, diagnostics_dir=self.folder) as session:
                with self.assertRaises(TranslationError) as caught:
                    session.translate(PROMPT, [])
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertEqual(len(records), 1)
        self.assertTrue(error.details['diagnostic_saved'])
        path = self.folder / (error.details['diagnostic_id'] + '.json')
        record = json.loads(path.read_text(encoding='utf-8'))
        self.assertNotIn('raw_base64', error.details)
        self.assertNotIn(KEY, str(error))
        self.assertNotIn(KEY.encode(), base64.b64decode(record['raw_base64']))
        return error, record

    def test_distinct_response_failures_are_preserved_before_cache(self):
        invalid = response_for('deepseek')
        invalid['choices'][0]['message']['content'] = '{broken'
        empty = response_for('deepseek')
        empty['choices'][0]['message']['content'] = None
        refused = response_for('deepseek')
        refused['choices'][0]['message']['refusal'] = 'no'
        truncated = response_for('deepseek')
        truncated['choices'][0]['finish_reason'] = 'length'
        for reply, code in ((b'not-json ' + KEY.encode(), 'api_json'),
                            (invalid, 'api_content_json'), (empty, 'api_empty'),
                            (refused, 'api_refusal'), (truncated, 'api_finish'),
                            (response_for('deepseek', []), 'api_envelope')):
            with self.subTest(code=code):
                error, record = self.failure(reply, code)
                self.assertEqual(record['http_status'], 200)
                self.assertTrue(record['response_received'])
                self.assertGreater(record['response_bytes'], 0)
                self.assertEqual(record['error_code'], code)
                if code in ('api_json', 'api_content_json'):
                    self.assertIn('json_position', error.details)
        self.assertEqual(len(list(self.folder.glob('*.json'))), 6)

    def test_non_200_invalid_json_and_partial_http_keep_actual_bytes(self):
        _, record = self.failure(b'Authorization: Bearer ' + KEY.encode(), 'authentication', status=401)
        self.assertEqual(record['http_status'], 401)
        self.assertTrue(record['redacted'])
        _, record = self.failure(response_for('deepseek'), 'api_incomplete', incomplete_http=True)
        self.assertEqual(json.loads(base64.b64decode(record['raw_base64'])), response_for('deepseek'))

    def test_drop_has_no_invented_response(self):
        _, record = self.failure(b'', 'api_transport', drop=True)
        self.assertFalse(record['response_received'])
        self.assertEqual(record['raw_base64'], '')
        self.assertIsNone(record['http_status'])

    def test_capture_cap_redaction_and_save_failure(self):
        reply = (b'{"api_key":"another-secret", "cookie":"private-cookie", "echo":"' +
                 KEY.encode() + b'"}' + b' ' * 100)
        _, record = self.failure(reply, 'api_envelope')
        raw = base64.b64decode(record['raw_base64'])
        self.assertNotIn(b'another-secret', raw)
        self.assertNotIn(b'private-cookie', raw)
        with patch.object(api_diagnostics, 'MAX_CAPTURE_BYTES', 32):
            _, record = self.failure(b'x' * 100, 'api_json')
        self.assertTrue(record['truncated'])
        self.assertEqual(record['captured_bytes'], 32)
        with mock_api(reply=b'bad') as (config, _, _, _), \
                patch.object(api_diagnostics, 'save_failure', side_effect=OSError('private-path-secret')):
            with ApiTranslationSession(config, KEY, diagnostics_dir=self.folder) as session:
                with self.assertRaises(TranslationError) as caught:
                    session.translate(PROMPT, [])
        self.assertEqual(caught.exception.code, 'api_json')
        self.assertFalse(caught.exception.details['diagnostic_saved'])
        self.assertNotIn('private-path-secret', str(caught.exception.details))

    def test_cancel_keeps_partial_body_and_does_not_wait_for_full_response(self):
        with mock_api(slow_body=True) as (config, _, started, _):
            event, errors = threading.Event(), []
            with ApiTranslationSession(config, KEY, diagnostics_dir=self.folder) as session:
                def run():
                    try:
                        session.translate(PROMPT, [], cancel_event=event)
                    except TranslationError as exc:
                        errors.append(exc)
                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(started.wait(2))
                time.sleep(.1)
                event.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors[0].code, 'cancelled')
        record = json.loads(next(self.folder.glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(base64.b64decode(record['raw_base64']), b'{')
        self.assertEqual(record['response_bytes'], 1)

    def test_success_never_requires_diagnostic_disk_and_failure_records_do_not_cache(self):
        with mock_api() as (config, records, _, _), \
                patch.object(api_diagnostics, 'save_failure', side_effect=AssertionError('成功不写诊断')):
            with ApiTranslationSession(config, KEY, diagnostics_dir=self.folder) as session:
                session.translate(PROMPT, [])
                session.translate(PROMPT, [])
        self.assertEqual(len(records), 2)
        self.assertFalse(self.folder.exists())

    def test_timeout_and_response_limit_leave_bounded_evidence(self):
        with mock_api(delay=.3) as (config, records, _, _):
            with ApiTranslationSession(config, KEY, diagnostics_dir=self.folder) as session:
                session.TIMEOUT_SECONDS = .1
                with self.assertRaises(TranslationError) as caught:
                    session.translate(PROMPT, [])
            self.assertEqual(caught.exception.code, 'timeout')
            self.assertTrue(caught.exception.details['diagnostic_saved'])
            self.assertEqual(len(records), 1)
        with mock_api() as (config, _, _, _):
            with ApiTranslationSession(config, KEY, diagnostics_dir=self.folder) as session:
                session.MAX_RESPONSE_BYTES = 16
                with self.assertRaises(TranslationError) as caught:
                    session.translate(PROMPT, [])
            self.assertEqual(caught.exception.code, 'api_too_large')
            self.assertEqual(caught.exception.details['response_bytes'], 17)

    def test_redaction_cannot_expand_saved_body_beyond_cap(self):
        with patch.object(api_diagnostics, 'MAX_CAPTURE_BYTES', 32):
            details = api_diagnostics.save_failure(self.folder, {'request_id': 'synthetic-cap', 'response_bytes': 32},
                                                   b'x' * 32, 'x')
        record = json.loads((self.folder / 'synthetic-cap.json').read_text(encoding='utf-8'))
        self.assertLessEqual(len(base64.b64decode(record['raw_base64'])), 32)
        self.assertTrue(details['truncated'])

    def test_json_escaped_secret_is_redacted_without_requiring_valid_response_json(self):
        escaped = b'synthetic\\u002dtest\\u002dkey'
        for index, raw in enumerate((
                b'{"echo":"' + escaped + b'","kept":"evidence"}',
                b'not-json {"echo":"' + escaped + b'"} unfinished',
                b'{"echo":"' + escaped + b'\xff"}',
                b'{"echo":"' + escaped + b'"',
                b'{"echo":"synthetic\\u002dtest\\u00',
                b'not-json echo:' + escaped,
                b'{"echo":"bad\\q ' + escaped + b'"}',
                b'{"echo":"raw\nnewline ' + escaped + b'"}',
                json.dumps({'content': '{"echo":"' + escaped.decode() + '"}'}).encode())):
            with self.subTest(index=index):
                request_id = f'escaped-secret-{index}'
                api_diagnostics.save_failure(self.folder, {'request_id': request_id, 'response_bytes': len(raw)}, raw, KEY)
                record = json.loads((self.folder / (request_id + '.json')).read_text(encoding='utf-8'))
                saved = base64.b64decode(record['raw_base64'])
                self.assertTrue(record['redacted'])
                self.assertIn(b'[REDACTED]', saved)
                self.assertNotIn(escaped, saved)
                self.assertNotIn(b'synthetic', saved)
                self.assertEqual(record['captured_bytes'], len(raw))
                if index == 0:
                    self.assertEqual(json.loads(saved), {'echo': '[REDACTED]', 'kept': 'evidence'})
                if index == 2:
                    self.assertIn(b'\xff', saved, 'Unrelated invalid UTF-8 bytes stay available as evidence.')
                if index == 4:
                    self.assertFalse(saved.endswith(b'"'), 'Truncated strings must not be closed by the diagnostic writer.')

    def test_json_quote_backslash_and_capture_boundary_use_synthetic_secret(self):
        secret = 'synthetic-"quoted\\key'
        raw = json.dumps({'echo': secret, 'kept': 'evidence'}).encode()
        api_diagnostics.save_failure(self.folder, {'request_id': 'escaped-quote', 'response_bytes': len(raw)}, raw, secret)
        record = json.loads((self.folder / 'escaped-quote.json').read_text(encoding='utf-8'))
        self.assertEqual(json.loads(base64.b64decode(record['raw_base64'])), {'echo': '[REDACTED]', 'kept': 'evidence'})
        raw = b'{"echo":"synthetic\\u002dtest\\u002dkey"}'
        limit = raw.index(b'\\u002dkey') + 4
        with patch.object(api_diagnostics, 'MAX_CAPTURE_BYTES', limit):
            details = api_diagnostics.save_failure(self.folder, {'request_id': 'escaped-cap', 'response_bytes': len(raw)}, raw, KEY)
        record = json.loads((self.folder / 'escaped-cap.json').read_text(encoding='utf-8'))
        saved = base64.b64decode(record['raw_base64'])
        self.assertTrue(details['truncated'])
        self.assertNotIn(b'synthetic', saved)
        self.assertIn(b'[REDACTED]', saved)
        self.assertLessEqual(len(saved), limit)

    def test_duplicate_json_failure_keeps_redacted_response_and_original_stage(self):
        raw = b'{"echo":"synthetic\\u002dtest\\u002dkey","duplicate":1,"duplicate":2}'
        error, record = self.failure(raw, 'api_json')
        self.assertEqual(error.details['stage'], 'outer_json')
        saved = base64.b64decode(record['raw_base64'])
        self.assertNotIn(KEY, json.loads(saved)['echo'])
        self.assertIn(b'"duplicate":1,"duplicate":2', saved)
