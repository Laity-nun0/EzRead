"""仅用合成数据与回环 HTTP 验证本次运行统计，不调用账户/余额服务。"""

import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

from codex_bridge import TranslationError
from ezread.api_usage import ApiUsage, numeric_usage
from ezread.translation_api import ApiTranslationSession
from tests.test_translation_api import KEY, mock_api, response_for


class ApiUsageTests(unittest.TestCase):
    def test_empty_is_unknown_and_snapshot_cannot_mutate_collector(self):
        usage = ApiUsage()
        snapshot = usage.snapshot()
        self.assertEqual(snapshot['scope'], 'application_run')
        self.assertEqual(snapshot['requests'], {'attempted': 0, 'succeeded': 0, 'failed': 0, 'cancelled': 0})
        self.assertEqual(snapshot['tokens'], {'input': None, 'output': None, 'total': None})
        self.assertIsNone(snapshot['account_balance'])
        self.assertIsNone(snapshot['last'])
        snapshot['requests']['attempted'] = 100
        self.assertEqual(usage.snapshot()['requests']['attempted'], 0)

    def test_aggregation_preserves_unknown_and_never_derives_total(self):
        usage = ApiUsage()
        first = usage.begin('openai', 'model-a', 'chat')
        usage.add_usage(first, {'input_tokens': 5, 'output_tokens': 0})
        usage.add_usage(first, {'input_tokens': 1000})
        usage.finish(first, 'success')
        usage.finish(first, 'failed', 'api_response')
        second = usage.begin('openai', 'model-a', 'overview')
        usage.finish(second, 'failed', 'quota')
        third = usage.begin('gemini', 'model-b', 'structure')
        usage.add_usage(third, {'totalTokenCount': 7})
        usage.finish(third, 'cancelled', 'cancelled')
        usage.add_usage(third, {'totalTokenCount': 1000})
        snapshot = usage.snapshot()
        self.assertEqual(snapshot['requests'], {'attempted': 3, 'succeeded': 1, 'failed': 1, 'cancelled': 1})
        self.assertEqual(snapshot['tokens'], {'input': 5, 'output': 0, 'total': 7})
        self.assertEqual(snapshot['usage_reported'], 2)
        by_provider = {row['provider']: row for row in snapshot['models']}
        self.assertEqual(by_provider['openai']['tokens'], {'input': 5, 'output': 0, 'total': None})
        self.assertEqual(by_provider['openai']['last']['code'], 'quota')
        self.assertEqual(by_provider['gemini']['last']['status'], 'cancelled')

    def test_numeric_fields_are_whitelisted_and_not_coerced_or_leaked(self):
        raw = {'usage': {'prompt_tokens': 10, 'completion_tokens': -1, 'total_tokens': '11',
                         'input_tokens': True, 'output_tokens': 1.5, KEY: 10,
                         'prompt_tokens_details': {'cached_tokens': 3, 'private': KEY},
                         'error': KEY}}
        self.assertEqual(numeric_usage(raw, 'deepseek'),
                         {'prompt_tokens': 10, 'prompt_tokens_details.cached_tokens': 3})
        usage = ApiUsage()
        request_id = usage.begin('deepseek', 'model-a', 'chat')
        usage.add_usage(request_id, {KEY: 10, 'total_tokens': 5, 'error': KEY})
        usage.finish(request_id, 'failed', KEY)
        snapshot = usage.snapshot()
        self.assertNotIn(KEY, json.dumps(snapshot))
        self.assertEqual(snapshot['last']['code'], 'unknown')

    def test_threadsafe_counts_and_each_application_has_its_own_collector(self):
        usage = ApiUsage()

        def run():
            for _ in range(20):
                request_id = usage.begin('deepseek', 'model-a', 'chat')
                usage.add_usage(request_id, {'total_tokens': 1})
                usage.finish(request_id, 'success')

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(usage.snapshot()['requests']['succeeded'], 80)
        self.assertEqual(usage.snapshot()['tokens']['total'], 80)
        self.assertEqual(ApiUsage().snapshot()['requests']['attempted'], 0)

    def test_models_under_one_provider_remain_separate(self):
        usage = ApiUsage()
        for model, count in (('first-model', 3), ('second-model', 7)):
            request_id = usage.begin('deepseek', model, 'chat')
            usage.add_usage(request_id, {'total_tokens': count})
            usage.finish(request_id, 'success')
        snapshot = usage.snapshot()
        self.assertEqual(snapshot['tokens']['total'], 10)
        self.assertEqual({row['model']: row['tokens']['total'] for row in snapshot['models']},
                         {'first-model': 3, 'second-model': 7})


class TransportUsageTests(unittest.TestCase):
    def test_five_provider_usage_is_recorded_without_secrets_urls_or_prompts(self):
        for provider in ('deepseek', 'openai_compatible', 'openai', 'anthropic', 'gemini'):
            reply = response_for(provider)
            field = 'usageMetadata' if provider == 'gemini' else 'usage'
            if provider == 'gemini':
                reply[field] = {'promptTokenCount': 11, 'candidatesTokenCount': 3, 'totalTokenCount': 14}
            elif provider in ('openai', 'anthropic'):
                reply[field] = {'input_tokens': 11, 'output_tokens': 3, 'total_tokens': 14}
            else:
                reply[field] = {'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}
            usage = ApiUsage()
            with self.subTest(provider=provider), mock_api(provider, reply=reply) as data:
                config, records, _, _ = data
                with ApiTranslationSession(config, KEY, usage=usage) as session:
                    session.translate('private-paper-prompt', [])
                    session.request_json('private-system', 'private-second-prompt')
                snapshot = usage.snapshot()
                self.assertEqual(snapshot['requests']['attempted'], len(records))
                self.assertEqual(snapshot['requests']['succeeded'], 2)
                self.assertEqual(snapshot['tokens'], {'input': 22, 'output': 6, 'total': 28})
                self.assertEqual(snapshot['usage_reported'], 2)
                self.assertEqual(snapshot['last']['channel'], 'pdf')
                serialized = json.dumps(snapshot)
                for secret in (KEY, config['base_url'], 'private-paper-prompt', 'private-system'):
                    self.assertNotIn(secret, serialized)
                self.assertEqual(records[0]['peer'], records[1]['peer'])

    def test_success_without_usage_remains_unknown(self):
        reply = response_for('deepseek')
        del reply['usage']
        with mock_api(reply=reply) as (config, _, _, _):
            usage = ApiUsage()
            with ApiTranslationSession(config, KEY, usage=usage) as session:
                session.translate('text', [])
            snapshot = usage.snapshot()
            self.assertEqual(snapshot['requests']['succeeded'], 1)
            self.assertEqual(snapshot['usage_reported'], 0)
            self.assertEqual(snapshot['tokens'], {'input': None, 'output': None, 'total': None})

    def test_auth_quota_server_and_truncation_keep_reported_usage_and_last_status(self):
        for status, code in ((401, 'authentication'), (403, 'authentication'), (429, 'quota'),
                             (500, 'api_server'), (200, 'api_finish')):
            reply = response_for('deepseek')
            reply['usage'] = {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}
            reply['error'] = {'message': KEY}
            reply['choices'][0]['finish_reason'] = 'length'
            with self.subTest(status=status), mock_api(status=status, reply=reply) as data:
                config, records, _, _ = data
                usage = ApiUsage()
                with ApiTranslationSession(config, KEY, usage=usage, channel='overview') as session:
                    with self.assertRaises(TranslationError) as error:
                        session.request_json('rules', 'text')
                self.assertEqual(error.exception.code, code)
                snapshot = usage.snapshot()
                self.assertEqual(snapshot['requests']['attempted'], 1)
                self.assertEqual(snapshot['requests']['failed'], 1)
                self.assertEqual(snapshot['tokens']['total'], 7)
                self.assertEqual(snapshot['last']['status'], 'failed')
                self.assertEqual(snapshot['last']['code'], code)
                self.assertNotIn(KEY, json.dumps(snapshot))
                self.assertEqual(len(records), 1)

    def test_bad_response_without_usage_is_failed_but_tokens_stay_unknown(self):
        with mock_api(reply=b'not json ' + KEY.encode()) as (config, records, _, _):
            usage = ApiUsage()
            with ApiTranslationSession(config, KEY, usage=usage) as session:
                with self.assertRaises(TranslationError):
                    session.translate('text', [])
            snapshot = usage.snapshot()
            self.assertEqual(snapshot['requests']['failed'], 1)
            self.assertIsNone(snapshot['tokens']['total'])
            self.assertEqual(len(records), 1)

    def test_pre_cancel_and_connection_failure_do_not_count_as_dispatched_requests(self):
        with mock_api() as (config, records, _, _):
            usage = ApiUsage()
            event = threading.Event()
            event.set()
            with ApiTranslationSession(config, KEY, usage=usage) as session:
                with self.assertRaises(TranslationError):
                    session.translate('text', [], cancel_event=event)
            with patch.object(http.client.HTTPConnection, 'connect', side_effect=OSError(KEY)):
                with ApiTranslationSession(config, KEY, usage=usage) as session:
                    with self.assertRaises(TranslationError):
                        session.translate('text', [])
            self.assertEqual(usage.snapshot()['requests']['attempted'], 0)
            self.assertIsNone(usage.snapshot()['last'])
            self.assertEqual(records, [])

    def test_cancel_during_connect_is_not_counted_even_when_connect_finishes_later(self):
        with mock_api() as (config, records, _, _):
            usage = ApiUsage()
            event = threading.Event()
            entered = threading.Event()
            release = threading.Event()
            finished = threading.Event()
            errors = []
            original_connect = http.client.HTTPConnection.connect

            def delayed_connect(connection):
                entered.set()
                release.wait(2)
                original_connect(connection)
                finished.set()

            with patch.object(http.client.HTTPConnection, 'connect', delayed_connect):
                with ApiTranslationSession(config, KEY, usage=usage) as session:
                    def translate():
                        try:
                            session.translate('text', [], cancel_event=event)
                        except TranslationError as exc:
                            errors.append(exc)

                    thread = threading.Thread(target=translate)
                    thread.start()
                    self.assertTrue(entered.wait(2))
                    event.set()
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    release.set()
                    self.assertTrue(finished.wait(2))
            self.assertEqual(errors[0].code, 'cancelled')
            self.assertEqual(usage.snapshot()['requests']['attempted'], 0)
            self.assertEqual(records, [])

    def test_inflight_pending_then_cancel_is_counted_once(self):
        with mock_api(slow_body=True) as (config, records, started, disconnected):
            usage = ApiUsage()
            event = threading.Event()
            errors = []
            with ApiTranslationSession(config, KEY, usage=usage, channel='chat') as session:
                def translate():
                    try:
                        session.request_json('rules', 'text', cancel_event=event)
                    except TranslationError as exc:
                        errors.append(exc)

                thread = threading.Thread(target=translate)
                thread.start()
                self.assertTrue(started.wait(2))
                self.assertEqual(usage.snapshot()['last']['status'], 'pending')
                event.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertTrue(disconnected.wait(1))
            snapshot = usage.snapshot()
            self.assertEqual(snapshot['requests'], {'attempted': 1, 'succeeded': 0, 'failed': 0, 'cancelled': 1})
            self.assertEqual(snapshot['last']['code'], 'cancelled')
            self.assertEqual(len(records), 1)
            self.assertEqual(errors[0].code, 'cancelled')
            time.sleep(0.05)
            self.assertEqual(usage.snapshot()['requests']['cancelled'], 1)


if __name__ == '__main__':
    unittest.main()
