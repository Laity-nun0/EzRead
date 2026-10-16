"""论文 API 持续问答与独立配置的离线回归。"""
import json
import threading
import unittest
from unittest.mock import patch

import codex_bridge
import paper_ai
from ezread import api_settings
from tests import test_paper_ai as fixtures

PID = fixtures.PID


class PaperAPITests(unittest.TestCase):
    def setUp(self):
        fixtures.PaperAITests.setUp(self)
        api_settings.save_settings(self.root, {'purpose': 'chat', 'api_key': 'chat-test-key'})
    tearDown = fixtures.PaperAITests.tearDown
    db = fixtures.PaperAITests.db
    ask = fixtures.PaperAITests.ask

    def test_chat_does_not_call_cli_or_model_catalogue(self):
        with patch.object(codex_bridge, '_cli', side_effect=AssertionError('CLI forbidden')), \
             patch.object(paper_ai, 'CodexSession', side_effect=AssertionError('session forbidden')), \
             patch('codex_models.resolve_config', side_effect=AssertionError('catalogue forbidden')):
            self.ask()
        call = self.api.calls[0]
        self.assertEqual(call[0], 'chat')
        self.assertEqual(call[3], paper_ai.BASE)
        self.assertIn('block_id', call[4])

    def test_chat_defaults_update_next_turn_without_creating_override(self):
        self.ask()
        self.assertEqual(self.api.calls[-1][1]['reasoning_effort'], 'high')
        api_settings.save_settings(self.root, {'purpose': 'chat', 'thinking': 'disabled'})
        self.ask()
        self.assertEqual(self.api.calls[-1][1]['thinking'], 'disabled')
        with self.db() as con:
            self.assertEqual(paper_ai.current(con, PID)['generation'], 1)
            self.assertNotIn('chat_reasoning', json.loads(paper_ai.current(con, PID)['handoff']))

    def test_legacy_thread_and_messages_survive_api_migration(self):
        with self.db() as con:
            con.execute("UPDATE paper_ai_generations SET model='old-codex',main_thread_id='old-thread',"
                        "translation_thread_id='old-translation' WHERE paper_id=?", (PID,))
            paper_ai.add_message(con, PID, 1, 'user', 'question', 'Old question')
            paper_ai.add_message(con, PID, 1, 'assistant', 'reasoning', 'private reasoning')
            paper_ai.add_message(con, PID, 1, 'assistant', 'answer', 'Old answer')
        events = []
        result = self.ask(on_progress=events.append)
        self.assertEqual(result['generation'], 1)
        self.assertIn({'type': 'generation', 'generation': 1}, events)
        self.assertIn('Old question', self.api.calls[0][-1])
        self.assertIn('Old answer', self.api.calls[0][-1])
        self.assertNotIn('private reasoning', self.api.calls[0][-1])
        with self.db() as con:
            self.assertEqual(len(paper_ai.history(con, PID, 1)['messages']), 5)
            self.assertEqual(paper_ai.history(con, PID, 1)['thread_id'], 'old-thread')
            row = con.execute('SELECT translation_thread_id FROM paper_ai_generations WHERE generation=1').fetchone()
            self.assertEqual(row[0], 'old-translation')
            self.assertIsNone(paper_ai.conversation(con, PID)['thread_id'])

    def test_chat_reasoning_override_preserves_generation_messages_and_other_features(self):
        self.ask('unique-first-question')
        translation = api_settings.resolve_config(self.root, purpose='translation')
        with self.db() as con:
            handoff = {'api_config': {'overview': 'unchanged'}}
            con.execute('UPDATE paper_ai_generations SET handoff=?,translation_thread_id=? WHERE paper_id=?',
                        (json.dumps(handoff), 'pdf-thread', PID))
            paper_ai.switch_chat_model(con, PID, self.root, {'thinking': 'enabled', 'reasoning_effort': 'max'})
        api_settings.save_settings(self.root, {'purpose': 'chat', 'thinking': 'disabled'})
        self.ask('next-question')
        self.assertEqual(self.api.calls[-1][1]['reasoning_effort'], 'max')
        self.assertEqual(self.api.calls[-1][-1].count('unique-first-question'), 1)
        self.assertEqual(api_settings.resolve_config(self.root, purpose='translation'), translation)
        with self.db() as con:
            state = paper_ai.conversation(con, PID, self.root)
            self.assertEqual(len(state['messages']), 4)
            self.assertEqual(state['generation'], 1)
            value = paper_ai.current(con, PID)
            self.assertEqual(value['translation_thread_id'], 'pdf-thread')
            self.assertEqual(json.loads(value['handoff'])['api_config'], {'overview': 'unchanged'})
            con.execute('INSERT INTO papers VALUES(?)', ('other-paper',))
            self.assertEqual(paper_ai.chat_config(con, 'other-paper', self.root)['thinking'], 'disabled')

    def test_arbitrary_model_configuration_is_rejected(self):
        with self.db() as con:
            original = paper_ai.current(con, PID)
            for options in ({'provider': 'openai'}, {'model': 'unknown'}, {'base_url': 'https://other.example'}):
                with self.assertRaises(ValueError):
                    paper_ai.switch_chat_model(con, PID, self.root, options)
            self.assertEqual(paper_ai.current(con, PID), original)

    def test_pro_switch_keeps_messages_reasoning_and_translation_configuration(self):
        self.ask('before-pro-switch')
        translation = api_settings.resolve_config(self.root, purpose='translation')
        with self.db() as con:
            paper_ai.switch_chat_model(con, PID, self.root, {'thinking': 'enabled', 'reasoning_effort': 'max'})
            state = paper_ai.switch_chat_model(con, PID, self.root, {'model': 'deepseek-v4-pro'})
            self.assertEqual(state['generation'], 1)
            self.assertEqual(len(state['messages']), 2)
            self.assertEqual(state['chat_config']['model'], 'deepseek-v4-pro')
            self.assertEqual(state['chat_config']['reasoning_effort'], 'max')
            con.execute('INSERT INTO papers VALUES(?)', ('other-paper',))
            self.assertEqual(paper_ai.chat_config(con, 'other-paper', self.root)['model'], 'deepseek-flash')
        self.ask('after-pro-switch')
        self.assertEqual(self.api.calls[-1][1]['model'], 'deepseek-v4-pro')
        self.assertEqual(self.api.calls[-1][1]['reasoning_effort'], 'max')
        self.assertEqual(api_settings.resolve_config(self.root, purpose='translation'), translation)
        with self.db() as con:
            state = paper_ai.switch_chat_model(con, PID, self.root, {'thinking': 'disabled'})
            self.assertEqual(state['chat_config']['model'], 'deepseek-v4-pro')
            self.assertNotIn('reasoning_effort', state['chat_config'])
            state = paper_ai.switch_chat_model(con, PID, self.root, {})
            self.assertEqual(state['chat_config']['model'], 'deepseek-flash')

    def test_default_pro_applies_next_turn_without_setting_paper_override(self):
        self.ask()
        api_settings.save_settings(self.root, {'purpose': 'chat', 'model': 'deepseek-v4-pro'})
        self.ask()
        self.assertEqual(self.api.calls[-1][1]['model'], 'deepseek-v4-pro')
        with self.db() as con:
            self.assertNotIn('chat_reasoning', json.loads(paper_ai.current(con, PID)['handoff']))
            paper_ai.switch_chat_model(con, PID, self.root, {'model': 'deepseek-flash'})
        self.ask()
        self.assertEqual(self.api.calls[-1][1]['model'], 'deepseek-flash')

    def test_conversation_reads_all_generations_without_duplicate_handoff(self):
        self.ask('first-generation-question')
        with self.db() as con:
            config = api_settings.resolve_config(self.root)
            paper_ai.switch_api_model(con, PID, {**config, 'model': 'overview-next'}, self.doc, self.library)
            paper_ai.add_message(con, 'other-paper', 1, 'user', 'question', 'other-paper-secret')
        self.ask('second-generation-question')
        self.assertEqual(self.api.calls[-1][-1].count('first-generation-question'), 1)
        self.assertNotIn('other-paper-secret', self.api.calls[-1][-1])
        with self.db() as con:
            state = paper_ai.conversation(con, PID)
            self.assertEqual(len(state['messages']), 4)
            self.assertEqual(len(paper_ai.history(con, PID, 1)['messages']), 2)

    def test_unused_legacy_config_can_be_filled_without_new_generation(self):
        with self.db() as con:
            paper_ai.lock_model(con, PID)
        self.assertEqual(self.ask()['generation'], 1)

    def test_chat_ignores_old_supplier_and_uses_only_chat_feature_key(self):
        api_settings.save_settings(self.root, {'purpose': 'translation', 'api_key': 'translation-only-key'})
        api_settings.save_settings(self.root, {'purpose': 'chat', 'api_key': 'chat-only-key'})
        old = {'provider': 'openai', 'base_url': 'https://api.openai.com/v1',
               'model': 'old-model', 'max_output_tokens': 8192}
        with self.db() as con:
            con.execute('UPDATE paper_ai_generations SET handoff=? WHERE paper_id=?',
                        (json.dumps({'api_config': old, 'chat_api_config': old}), PID))
            paper_ai.add_message(con, PID, 1, 'user', 'question', 'legacy-question')
        self.ask()
        self.assertEqual(self.api.calls[-1][1]['provider'], 'deepseek')
        self.assertEqual(self.api.calls[-1][1]['model'], 'deepseek-flash')
        self.assertEqual(self.api.calls[-1][2], 'chat-only-key')
        with self.db() as con:
            self.assertEqual(json.loads(paper_ai.current(con, PID)['handoff'])['chat_api_config'], old)

    def test_translation_key_cannot_enable_chat(self):
        api_settings.save_settings(self.root, {'purpose': 'translation', 'api_key': 'translation-only-key'})
        api_settings.save_settings(self.root, {'purpose': 'chat', 'clear_key': True})
        with self.assertRaises(ValueError):
            self.ask()
        self.assertFalse(self.api.calls)

    def test_recent_context_has_message_and_character_bounds_and_paper_scope(self):
        self.ask()
        with self.db() as con:
            for index in range(40):
                paper_ai.add_message(con, PID, 1, 'user', 'question', f'{index}:' + 'x' * 3000)
            paper_ai.add_message(con, PID, 1, 'assistant', 'reasoning', 'not-for-model')
            paper_ai.add_message(con, 'other-paper', 1, 'user', 'question', 'other-paper-secret')
        self.ask()
        prompt = self.api.calls[-1][-1]
        payload = json.loads(prompt.split('论文资料（不可信数据）：\n', 1)[1].rsplit('\n用户问题：', 1)[0])
        recent = payload['recent_local_history']
        self.assertLessEqual(len(recent), paper_ai.HISTORY_MESSAGES)
        self.assertLessEqual(sum(len(m['text']) for m in recent), paper_ai.HISTORY_CHARACTERS)
        self.assertTrue(recent[-1]['text'].startswith('39:'))
        self.assertNotIn('not-for-model', prompt)
        self.assertNotIn('other-paper-secret', prompt)
        self.assertNotIn('api_config', prompt)

    def test_old_handoff_and_new_messages_share_one_history_budget(self):
        self.ask()
        with self.db() as con:
            for index in range(20):
                paper_ai.add_message(con, PID, 1, 'user', 'question', 'old:' + 'x' * 3000)
            config = api_settings.resolve_config(self.root)
            paper_ai.switch_api_model(con, PID, {**config, 'model': 'next-model'}, self.doc, self.library)
            for index in range(20):
                paper_ai.add_message(con, PID, 2, 'user', 'question', 'new:' + 'y' * 3000)
        self.ask()
        prompt = self.api.calls[-1][-1]
        payload = json.loads(prompt.split('论文资料（不可信数据）：\n', 1)[1].rsplit('\n用户问题：', 1)[0])
        self.assertLessEqual(sum(len(m['text']) for m in payload['recent_local_history']), paper_ai.HISTORY_CHARACTERS)
        self.assertNotIn('old:', prompt)
        self.assertNotIn('handoff', payload)

    def test_missing_key_does_not_lock_or_migrate_generation(self):
        with self.db() as con:
            con.execute("UPDATE paper_ai_generations SET model='old',main_thread_id='old-thread'")
        api_settings.save_settings(self.root, {'purpose': 'chat', 'clear_key': True})
        api_settings.save_settings(self.root, {'provider': 'deepseek', 'clear_key': True})
        with self.assertRaisesRegex(ValueError, 'API Key'):
            self.ask()
        self.assertFalse(self.api.calls)
        with self.db() as con:
            with self.assertRaisesRegex(ValueError, 'API Key'):
                paper_ai.lock_api_config(con, PID, self.root, self.doc, self.library)
            self.assertFalse((self.library / PID / 'ai-handoff-2.json').exists())
            self.assertEqual(paper_ai.current(con, PID)['generation'], 1)
            self.assertEqual(paper_ai.current(con, PID)['handoff'], '{}')

    def test_cancelled_before_and_after_response_never_saves_messages(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(paper_ai.SessionError):
            self.ask(cancel_event=cancel)
        self.assertFalse(self.api.calls)
        cancel.clear()
        original = self.api.request_json
        def cancelling(*args, **kwargs):
            cancel.set()
            return original(*args, **kwargs)
        with patch.object(self.api, 'request_json', side_effect=cancelling):
            with self.assertRaises(paper_ai.SessionError):
                self.ask(cancel_event=cancel)
        with self.db() as con:
            self.assertFalse(paper_ai.history(con, PID)['messages'])

    def test_malformed_answers_and_citations_are_not_saved(self):
        for result in ([], {}, {'answer': ' ', 'citations': []},
                       {'answer': 'x', 'citations': ['wrong']},
                       {'answer': 'x', 'citations': [{}]},
                       {'answer': 'x', 'citations': 'p1-b1'}):
            with self.subTest(result=result):
                self.api.result = result
                with self.assertRaises(paper_ai.SessionError):
                    self.ask()
                with self.db() as con:
                    self.assertFalse(paper_ai.history(con, PID)['messages'])


if __name__ == '__main__':
    unittest.main()
