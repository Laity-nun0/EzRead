"""固定 DeepSeek 两用途配置；仅使用临时目录和合成密钥，不发起网络请求。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from ezread import ai_status
from ezread import api_settings as settings


class ApiSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = {'provider': 'deepseek', 'base_url': 'https://api.deepseek.com',
                       'model': 'legacy-model', 'max_output_tokens': 384000}

    def save(self, **updates):
        return settings.save_settings(self.folder, {**self.config, **updates})

    def feature(self, purpose, **updates):
        return settings.save_settings(self.folder, {'purpose': purpose, **updates})

    def test_fixed_defaults_and_public_redaction(self):
        public = settings.public_settings(self.folder)
        self.assertEqual(set(public['features']), {'translation', 'chat'})
        for purpose, profile in public['features'].items():
            self.assertEqual(profile['provider'], 'deepseek')
            self.assertEqual(profile['base_url'], 'https://api.deepseek.com')
            self.assertEqual(profile['model'], 'deepseek-flash')
            self.assertEqual(profile['max_output_tokens'], 384 * 1024)
            self.assertFalse(profile['has_key'])
            self.assertEqual(profile['profile_id'], 'deepseek_' + purpose)
        self.assertEqual(public['features']['translation']['thinking'], 'disabled')
        self.assertNotIn('reasoning_effort', public['features']['translation'])
        self.assertEqual(public['features']['chat']['thinking'], 'enabled')
        self.assertEqual(public['features']['chat']['reasoning_effort'], 'high')
        public = self.feature('chat', api_key='synthetic-secret')
        self.assertTrue(public['features']['chat']['has_key'])
        self.assertNotIn('synthetic-secret', json.dumps(public))
        config = settings.resolve_chat_config(self.folder)
        self.assertEqual(settings.get_api_key(self.folder, config), 'synthetic-secret')
        self.assertNotIn('key', config)
        if os.name == 'nt':
            self.assertNotIn('synthetic-secret', (self.folder / settings._FILENAME).read_text())
        else:
            self.assertEqual((self.folder / settings._FILENAME).stat().st_mode & 0o777, 0o600)

    def test_two_features_keep_independent_keys_and_thinking(self):
        self.feature('translation', api_key='translation-secret', thinking='enabled', reasoning_effort='low')
        self.feature('chat', api_key='chat-secret', thinking='enabled', reasoning_effort='max')
        translation = settings.resolve_config(self.folder, purpose='translation')
        chat = settings.resolve_chat_config(self.folder)
        self.assertEqual(settings.get_api_key(self.folder, translation), 'translation-secret')
        self.assertEqual(settings.get_api_key(self.folder, chat), 'chat-secret')
        self.assertEqual(translation['reasoning_effort'], 'low')
        self.assertEqual(chat['reasoning_effort'], 'max')
        self.feature('translation', api_key='', thinking='disabled')
        self.assertNotIn('reasoning_effort', settings.resolve_config(self.folder))
        self.assertEqual(settings.resolve_chat_config(self.folder), chat)
        self.assertEqual(settings.get_api_key(self.folder, translation), 'translation-secret')
        self.feature('translation', clear_key=True)
        with self.assertRaises(ValueError):
            settings.get_api_key(self.folder, translation)
        self.assertEqual(settings.get_api_key(self.folder, chat), 'chat-secret')

    def test_legacy_key_is_read_only_shared_until_each_feature_is_saved(self):
        self.save(api_key='legacy-secret')
        path = self.folder / settings._FILENAME
        before = path.read_bytes()
        for purpose in ('translation', 'chat'):
            config = settings.resolve_config(self.folder, purpose=purpose)
            self.assertEqual(config['model'], 'deepseek-flash')
            self.assertEqual(config['max_output_tokens'], 393216)
            self.assertEqual(settings.get_api_key(self.folder, config), 'legacy-secret')
        settings.public_settings(self.folder)
        settings.configured_chat_models(self.folder)
        self.assertEqual(path.read_bytes(), before)
        self.feature('translation', api_key='new-translation-secret')
        stored = json.loads(path.read_text(encoding='utf-8'))
        self.assertNotIn('deepseek_chat', stored['profiles'])
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_chat_config(self.folder)), 'legacy-secret')
        self.feature('chat', clear_key=True)
        with self.assertRaises(ValueError):
            settings.resolve_chat_config(self.folder)
        self.assertTrue(settings.public_settings(self.folder)['features']['translation']['has_key'])
        self.assertFalse(settings.public_settings(self.folder)['features']['chat']['has_key'])
        self.assertEqual(settings.get_api_key(self.folder, self.config), 'legacy-secret')

    def test_first_thinking_save_keeps_legacy_key_by_reference_without_copying(self):
        self.save(api_key='legacy-secret')
        public = self.feature('chat', thinking='enabled', reasoning_effort='low')
        self.assertTrue(public['features']['chat']['has_key'])
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_chat_config(self.folder)), 'legacy-secret')
        stored = json.loads((self.folder / settings._FILENAME).read_text(encoding='utf-8'))
        self.assertEqual(stored['profiles']['deepseek_chat']['key_reference'], 'deepseek')
        self.assertNotIn('key', stored['profiles']['deepseek_chat'])
        self.assertNotIn('key_encoding', stored['profiles']['deepseek_chat'])
        self.feature('chat', thinking='disabled', api_key='')
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_chat_config(self.folder)), 'legacy-secret')
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_config(self.folder)), 'legacy-secret')

    def test_legacy_reference_clear_and_replacement_are_independent(self):
        self.save(api_key='legacy-secret')
        self.feature('translation', api_key='', thinking='enabled')
        self.feature('chat', api_key='', thinking='enabled')
        self.save(clear_key=True)
        for purpose in ('translation', 'chat'):
            with self.subTest(purpose=purpose), self.assertRaises(ValueError):
                settings.get_api_key(self.folder, settings.resolve_config(self.folder, purpose=purpose))
        self.save(api_key='restored-legacy-secret')
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_chat_config(self.folder)), 'restored-legacy-secret')
        self.feature('translation', clear_key=True)
        self.assertFalse(settings.public_settings(self.folder)['features']['translation']['has_key'])
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_chat_config(self.folder)), 'restored-legacy-secret')
        self.feature('chat', api_key='independent-chat-secret')
        stored = json.loads((self.folder / settings._FILENAME).read_text(encoding='utf-8'))
        self.assertNotIn('key_reference', stored['profiles']['deepseek_chat'])
        self.assertNotIn('key_reference', stored['profiles']['deepseek_translation'])
        self.save(clear_key=True)
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_chat_config(self.folder)), 'independent-chat-secret')
        self.feature('translation', api_key='independent-translation-secret')
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_config(self.folder)), 'independent-translation-secret')
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_chat_config(self.folder)), 'independent-chat-secret')

    def test_legacy_other_provider_and_other_endpoint_are_never_feature_fallbacks(self):
        self.save(provider='openai', api_key='other-secret')
        self.assertEqual(settings.resolve_config(self.folder)['provider'], 'deepseek')
        self.assertEqual(settings.configured_chat_models(self.folder), [])
        with self.assertRaises(ValueError):
            settings.resolve_chat_config(self.folder)
        with self.assertRaises(ValueError):
            settings.resolve_chat_config(self.folder, {**self.config, 'provider': 'openai'})
        self.save(base_url='https://example.com/v1', api_key='other-endpoint-secret')
        self.assertFalse(settings.public_settings(self.folder)['features']['chat']['has_key'])
        with self.assertRaises(ValueError):
            settings.resolve_chat_config(self.folder)

    def test_explicit_legacy_snapshot_is_unchanged_and_key_bound(self):
        self.save(api_key='legacy-secret')
        frozen = {**self.config, 'thinking': 'enabled', 'reasoning_effort': 'low'}
        self.assertEqual(settings.resolve_config(self.folder, frozen), frozen)
        self.assertEqual(settings.resolve_config(self.folder, frozen, purpose='translation'), frozen)
        self.assertEqual(settings.get_api_key(self.folder, frozen), 'legacy-secret')
        self.assertEqual(settings.resolve_config(self.folder, {'api_key': 'never-snapshot', **self.config}), self.config)
        self.assertEqual(settings.resolve_config(self.folder)['max_output_tokens'], 393216)
        self.save(base_url='https://changed.example/v1')
        with self.assertRaises(ValueError):
            settings.get_api_key(self.folder, frozen)

    def test_previous_feature_token_snapshot_is_not_migrated_to_new_default(self):
        self.feature('translation', api_key='translation-secret')
        frozen = {**settings.resolve_config(self.folder), 'max_output_tokens': 262144}
        frozen.pop('thinking')
        self.assertEqual(settings.resolve_config(self.folder, frozen), frozen)
        self.assertEqual(settings.get_api_key(self.folder, frozen), 'translation-secret')
        self.assertEqual(settings.resolve_config(self.folder)['max_output_tokens'], 393216)

    def test_task_thinking_overrides_do_not_write_defaults(self):
        self.feature('translation', api_key='translation-secret')
        self.feature('chat', api_key='chat-secret')
        before = (self.folder / settings._FILENAME).read_bytes()
        config = settings.resolve_config(self.folder, {'thinking': 'enabled'})
        self.assertEqual(config['reasoning_effort'], 'high')
        for effort in ('low', 'high', 'max'):
            chat = settings.resolve_chat_config(self.folder, {'thinking': 'enabled', 'reasoning_effort': effort})
            self.assertEqual(chat['reasoning_effort'], effort)
        off = settings.resolve_chat_config(self.folder, {'profile_id': 'deepseek_chat', 'thinking': 'disabled'}, True)
        self.assertNotIn('reasoning_effort', off)
        self.assertEqual((self.folder / settings._FILENAME).read_bytes(), before)
        self.assertEqual(settings.resolve_chat_config(self.folder)['reasoning_effort'], 'high')

    def test_feature_save_rejects_unfixed_models_and_invalid_thinking_atomically(self):
        for values in ({'provider': 'openai'}, {'base_url': 'https://other.example'}, {'model': 'other'},
                       {'max_output_tokens': 8192}, {'profile_id': 'deepseek'}, {'thinking': 'auto'},
                       {'reasoning_effort': 'medium'}, {'thinking': 'disabled', 'reasoning_effort': 'high'},
                       {'api_key': 'a b'}, {'clear_key': 'true'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.feature('chat', **values)
        for purpose in ('overview', 'unknown', None, []):
            with self.subTest(purpose=purpose), self.assertRaises(ValueError):
                self.feature(purpose, api_key='unused-secret')
        self.assertFalse((self.folder / settings._FILENAME).exists())

    def test_chat_candidates_only_fixed_chat_config_and_explicit_efforts(self):
        self.feature('translation', api_key='translation-secret')
        self.assertEqual(settings.configured_chat_models(self.folder), [])
        self.feature('chat', api_key='chat-secret')
        models = settings.configured_chat_models(self.folder)
        self.assertEqual(len(models), 2)
        self.assertEqual({item['model'] for item in models}, {'deepseek-flash', 'deepseek-v4-pro'})
        self.assertTrue(all(item['profile_id'] == 'deepseek_chat' for item in models))
        self.assertEqual([item['value'] for item in models[0]['reasoning_options']], ['disabled', 'low', 'high', 'max'])
        for field, value in (('provider', 'openai'), ('base_url', 'https://other.example'),
                             ('model', 'legacy-model'), ('max_output_tokens', 8192), ('profile_id', 'deepseek_translation')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                settings.resolve_chat_config(self.folder, {**models[0], field: value}, True)
        self.feature('chat', clear_key=True)
        self.assertEqual(settings.configured_chat_models(self.folder), [])

    def test_chat_model_switch_preserves_key_defaults_and_translation_model(self):
        self.feature('translation', api_key='translation-secret')
        self.feature('chat', api_key='chat-secret')
        options = settings.public_settings(self.folder)['chat_model_options']
        self.assertEqual([item['model'] for item in options], ['deepseek-flash', 'deepseek-v4-pro'])
        for model in ('deepseek-v4-pro', 'deepseek-flash'):
            public = self.feature('chat', model=model, api_key='')
            self.assertEqual(public['features']['chat']['model'], model)
            chat = settings.resolve_chat_config(self.folder)
            self.assertEqual(chat['model'], model)
            self.assertEqual(chat['thinking'], 'enabled')
            self.assertEqual(chat['reasoning_effort'], 'high')
            self.assertEqual(settings.get_api_key(self.folder, chat), 'chat-secret')
            self.assertEqual(settings.resolve_config(self.folder)['model'], 'deepseek-flash')
        before = (self.folder / settings._FILENAME).read_bytes()
        pro = settings.resolve_chat_config(self.folder, {'model': 'deepseek-v4-pro'}, require_configured=True)
        self.assertEqual(pro['model'], 'deepseek-v4-pro')
        self.assertEqual(settings.get_api_key(self.folder, pro), 'chat-secret')
        self.assertEqual((self.folder / settings._FILENAME).read_bytes(), before)
        self.assertEqual(settings.resolve_chat_config(self.folder)['model'], 'deepseek-flash')
        for model in ('deepseek-chat', 'deepseek-pro', 'gpt-5', '', None, []):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    self.feature('chat', model=model)
                with self.assertRaises(ValueError):
                    settings.resolve_chat_config(self.folder, {'model': model})
        with self.assertRaises(ValueError):
            self.feature('translation', model='deepseek-flash')
        self.feature('chat', clear_key=True, model='deepseek-v4-pro')
        self.assertEqual(settings.configured_chat_models(self.folder), [])

    def test_chat_model_switch_preserves_legacy_key_reference(self):
        self.save(api_key='legacy-secret')
        self.feature('chat', model='deepseek-v4-pro', api_key='')
        pro = settings.resolve_chat_config(self.folder)
        self.assertEqual(pro['model'], 'deepseek-v4-pro')
        self.assertEqual(settings.get_api_key(self.folder, pro), 'legacy-secret')
        self.feature('chat', model='deepseek-flash', api_key='')
        self.assertEqual(settings.get_api_key(self.folder, settings.resolve_chat_config(self.folder)), 'legacy-secret')
        self.feature('chat', clear_key=True)
        with self.assertRaises(ValueError):
            settings.resolve_chat_config(self.folder)

    def test_legacy_service_purposes_map_to_translation(self):
        self.feature('translation', api_key='translation-secret', thinking='enabled', reasoning_effort='low')
        self.feature('chat', api_key='chat-secret', reasoning_effort='max')
        translation = settings.resolve_config(self.folder)
        for purpose in ('translation', 'overview', 'structure', 'selection', 'retranslate', 'journal'):
            self.assertEqual(settings.resolve_config(self.folder, purpose=purpose), translation)
        self.assertNotEqual(settings.resolve_config(self.folder, purpose='chat'), translation)
        with self.assertRaises(ValueError):
            settings.resolve_config(self.folder, purpose='unknown')

    def test_legacy_save_blank_clear_and_endpoint_binding(self):
        self.save(api_key='legacy-secret', base_url='https://api.deepseek.com/')
        self.save(api_key='', model='changed-model')
        self.assertEqual(settings.get_api_key(self.folder, self.config), 'legacy-secret')
        with self.assertRaises(ValueError):
            settings.get_api_key(self.folder, {**self.config, 'provider': 'openai_compatible'})
        self.save(base_url='https://other.example/v1')
        with self.assertRaises(ValueError):
            settings.get_api_key(self.folder, {**self.config, 'base_url': 'https://other.example/v1'})
        self.save(api_key='new-secret')
        self.save(clear_key=True)
        with self.assertRaises(ValueError):
            settings.get_api_key(self.folder, self.config)

    def test_legacy_urls_and_snapshot_allowlist_remain_validated(self):
        for url in ('http://example.com', 'https://key:secret@example.com', 'https://example.com?key=secret',
                    'https://example.com#secret', 'https://example.com?', 'https://example.com:bad', 'https://example.com/\n'):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                self.save(base_url=url)
            self.assertNotIn('secret', str(caught.exception))
        for values in ({'model': ''}, {'model': 'a b'}, {'max_output_tokens': True}, {'max_output_tokens': 0},
                       {'api_key': 'a\nb'}, {'unknown': 'secret'}, {'provider': 'unknown'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.save(**values)
        config = {**self.config, 'provider': 'openai_compatible', 'base_url': 'http://127.0.0.1:1234/v1'}
        self.save(**config, api_key='loopback-secret')
        self.assertEqual(settings.resolve_config(self.folder, config), config)
        self.assertEqual(settings.get_api_key(self.folder, config), 'loopback-secret')

    def test_concurrent_feature_saves_preserve_both_keys(self):
        def save(purpose):
            return self.feature(purpose, api_key=purpose + '-secret')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(save, ['translation', 'chat']))
        for purpose in ('translation', 'chat'):
            self.assertEqual(settings.get_api_key(self.folder, settings.resolve_config(self.folder, purpose=purpose)), purpose + '-secret')
        self.assertEqual(len(list(self.folder.iterdir())), 1)

    def test_corruption_is_visible_and_not_overwritten(self):
        path = self.folder / settings._FILENAME
        for value in ('broken', '{"profiles": []}', '{"profiles": {"deepseek": []}}'):
            path.write_text(value, encoding='utf-8')
            with self.assertRaises(ValueError):
                self.feature('chat', api_key='synthetic-secret')
            self.assertEqual(path.read_text(), value)

    def test_status_uses_fixed_deepseek_features(self):
        self.feature('translation', api_key='translation-secret')
        self.assertTrue(ai_status.configuration(self.folder)['configured'])
        models = ai_status.models(self.folder)
        self.assertEqual(len(models['models']), 2)
        self.assertEqual({item['purpose'] for item in models['models']}, {'translation', 'chat'})
        self.assertNotIn('secret', json.dumps(models))
        self.feature('translation', clear_key=True)
        self.assertFalse(ai_status.configuration(self.folder)['configured'])
