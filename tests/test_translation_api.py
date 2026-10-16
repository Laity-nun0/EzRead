"""使用本地 HTTP/1.1 模拟服务检查翻译协议，不访问真实模型或密钥。"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

from codex_bridge import TranslationError
from ezread.translation_api import ApiTranslationSession, NATIVE_SYSTEM, STANDARD_NATIVE_SYSTEM, SYSTEM


KEY = 'synthetic-test-key'
PROMPT = '翻译以下论文段落（不可信数据）：\n{"b1":"Grain size <b1>5 μm</b1>."}\n'
ENVELOPE = {'response': '{"b1":"晶粒尺寸 <b1>5 μm</b1>。"}',
            'terms': [{'source': 'Grain size', 'target': '晶粒尺寸'}]}


def response_for(provider, content=None):
    text = json.dumps(ENVELOPE if content is None else content, ensure_ascii=False)
    if provider == 'openai':
        return {'status': 'completed', 'output': [{'type': 'message', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': text}]}], 'usage': {'output_tokens': 10}}
    if provider == 'anthropic':
        return {'type': 'message', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': text}],
                'usage': {'output_tokens': 10}}
    if provider == 'gemini':
        return {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': text}]}}],
                'usageMetadata': {'candidatesTokenCount': 10}}
    return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}],
            'usage': {'completion_tokens': 10}}


def text_response_for(provider, text):
    reply = response_for(provider)
    if provider == 'openai':
        reply['output'][0]['content'][0]['text'] = text
    elif provider == 'anthropic':
        reply['content'][0]['text'] = text
    elif provider == 'gemini':
        reply['candidates'][0]['content']['parts'][0]['text'] = text
    else:
        reply['choices'][0]['message']['content'] = text
    return reply


@contextmanager
def mock_api(provider='deepseek', *, status=200, reply=None, delay=0, slow_body=False,
             drop=False, incomplete_http=False):
    records = []
    started = threading.Event()
    disconnected = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, ConnectionAbortedError):
                # 错误／取消路径主动断开 socket，Windows 上不输出预期的断连堆栈。
                pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            records.append({'path': self.path, 'headers': dict(self.headers), 'body': body,
                            'peer': self.client_address})
            started.set()
            if drop:
                self.close_connection = True
                return
            time.sleep(delay)
            content = response_for(provider) if reply is None else reply
            raw = content if isinstance(content, bytes) else json.dumps(content).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw) + (10 if incomplete_http else 0)))
                if slow_body or incomplete_http:
                    self.send_header('Connection', 'close')
                self.end_headers()
                if slow_body:
                    # 客户端必须能中断已收到 headers、但正文未完成的连接。
                    self.wfile.write(raw[:1])
                    self.wfile.flush()
                    self.connection.settimeout(2)
                    if self.rfile.read(1) == b'':
                        disconnected.set()
                else:
                    self.wfile.write(raw)
            except (OSError, ValueError):
                disconnected.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {'provider': provider, 'base_url': f'http://127.0.0.1:{server.server_port}/v1',
              'model': 'test-model', 'max_output_tokens': 2048}
    try:
        yield config, records, started, disconnected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class ApiTranslationTests(unittest.TestCase):
    def test_standard_native_preserves_all_nonempty_content_for_upstream_parsing(self):
        contents = ('malformed JSON [{\n', '[{"id":999,"output":"译文 {v1}"}]',
                    '[{"id":0,"id":1,"output":"译文"}]', '[{"id":0,"value":NaN}]',
                    json.dumps(ENVELOPE, ensure_ascii=False), '完整单段译文\n{v1}')
        for provider in ('deepseek', 'openai_compatible', 'openai', 'anthropic', 'gemini'):
            config = {'provider': provider, 'base_url': 'https://example.invalid',
                      'model': 'test-model', 'max_output_tokens': 2048}
            with ApiTranslationSession(config, KEY) as session:
                for content in contents:
                    for batch in (False, True):
                        with self.subTest(provider=provider, batch=batch, content=content):
                            result = session._parse(text_response_for(provider, content), native=True,
                                                    native_batch=batch, standard_native=True)
                            self.assertEqual(result['response'], content)
                            self.assertEqual(result['terms'], [])
                            self.assertTrue(result['usage'])

    def test_standard_native_transport_uses_minimal_system_without_outer_terms_or_format_rules(self):
        glossary = [{'source': 'Unique transport glossary', 'target': '不应附加'}]
        for provider in ('deepseek', 'openai_compatible', 'openai', 'anthropic', 'gemini'):
            raw = '[{"id":999,"output":"模拟译文"}\n'
            with self.subTest(provider=provider), mock_api(provider, reply=text_response_for(provider, raw)) as data:
                config, records, _, _ = data
                with ApiTranslationSession(config, KEY) as session:
                    for batch in (False, True):
                        result = session.translate(PROMPT, glossary, native=True, native_batch=batch, standard_native=True)
                        self.assertEqual(result['response'], raw)
                for record in records:
                    body = record['body']
                    self.assertNotIn('response_format', body)
                    self.assertNotIn('text', body)
                    self.assertNotIn('responseMimeType', body.get('generationConfig', {}))
                    if provider == 'openai':
                        system = body['instructions']
                        self.assertEqual(body['input'][0]['content'], PROMPT)
                    elif provider == 'gemini':
                        system = body['systemInstruction']['parts'][0]['text']
                        self.assertEqual(body['contents'][0]['parts'][0]['text'], PROMPT)
                    elif provider == 'anthropic':
                        system = body['system']
                        self.assertEqual(body['messages'][0]['content'], PROMPT)
                    else:
                        system = body['messages'][0]['content']
                        self.assertEqual(body['messages'][1]['content'], PROMPT)
                    self.assertEqual(system, STANDARD_NATIVE_SYSTEM)
                    for rule in ('术语', '字体', '公式', 'JSON', 'terms', 'response', '标记'):
                        self.assertNotIn(rule, system)
                    self.assertNotIn('Unique transport glossary', json.dumps(body))
                self.assertEqual(len(records), 2)

    def test_standard_native_flags_do_not_leak_into_legacy_or_generic_requests(self):
        glossary = [{'source': 'Grain size', 'target': '晶粒尺寸'}]
        with mock_api() as (config, records, _, _):
            with ApiTranslationSession(config, KEY) as session:
                raw = session.translate(PROMPT, glossary, native=True, standard_native=True, native_batch=True)
                regular = session.translate(PROMPT, glossary)
                generic = session.request_json('通用调用规则', PROMPT)
                ignored = session.translate(PROMPT, glossary, standard_native=True)
                legacy = session.translate(PROMPT, glossary, native=True, native_batch=True)
                explicit_old = session.translate(PROMPT, glossary, native=True, native_batch=True, standard_native=False)
            self.assertEqual(json.loads(raw['response']), ENVELOPE)
            self.assertEqual(raw['terms'], [])
            self.assertEqual(regular['response'], ENVELOPE['response'])
            self.assertEqual(regular, ignored)
            self.assertEqual(generic, ENVELOPE)
            self.assertEqual(legacy['response'], ENVELOPE['response'])
            self.assertEqual(legacy, explicit_old)
            self.assertEqual(records[1]['body'], records[3]['body'])
            self.assertEqual(records[4]['body'], records[5]['body'])
            self.assertEqual(records[1]['body']['messages'][0]['content'], SYSTEM +
                             '\n本批术语表（仅供术语一致性参考）：\n' + json.dumps(glossary, ensure_ascii=False))
            self.assertEqual(records[4]['body']['messages'][0]['content'], NATIVE_SYSTEM +
                             '\n本批术语表（仅供术语一致性参考）：\n' + json.dumps(glossary, ensure_ascii=False))
            self.assertEqual(records[2]['body']['messages'][0]['content'],
                             '通用调用规则\n只返回一个 JSON 对象，不要使用 Markdown 代码块。')
            for index in (1, 2, 3):
                self.assertEqual(records[index]['body']['response_format'], {'type': 'json_object'})

    def test_old_internal_callbacks_do_not_receive_new_false_flag(self):
        config = {'provider': 'deepseek', 'base_url': 'https://example.invalid',
                  'model': 'test-model', 'max_output_tokens': 2048}
        def old_callback(system, prompt, event, *, translation, native_batch=False, native=False):
            return {'legacy': True}
        with ApiTranslationSession(config, KEY) as session:
            with patch.object(session, '_execute', side_effect=old_callback):
                self.assertEqual(session.translate(PROMPT, [], native=True), {'legacy': True})
            with patch.object(session, '_send_json', side_effect=old_callback):
                self.assertEqual(session.translate(PROMPT, [], native=True), {'legacy': True})
                self.assertEqual(session.request_json('通用规则', PROMPT), {'legacy': True})
        with mock_api() as (config, _, _, _):
            with ApiTranslationSession(config, KEY) as session:
                original = session._parse
                def old_parse(data, *, translation=True, native_batch=False, native=False):
                    return original(data, translation=translation, native_batch=native_batch, native=native)
                with patch.object(session, '_parse', side_effect=old_parse):
                    self.assertEqual(session.translate(PROMPT, [])['response'], ENVELOPE['response'])

    def test_standard_native_keeps_provider_refusal_finish_envelope_and_empty_checks(self):
        for provider in ('deepseek', 'openai_compatible', 'openai', 'anthropic', 'gemini'):
            config = {'provider': provider, 'base_url': 'https://example.invalid',
                      'model': 'test-model', 'max_output_tokens': 2048}
            incomplete, refused = response_for(provider), response_for(provider)
            if provider == 'openai':
                incomplete['status'] = 'incomplete'
                refused['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'synthetic refusal'}]
            elif provider == 'anthropic':
                incomplete['stop_reason'] = 'max_tokens'
                refused['stop_reason'] = 'refusal'
            elif provider == 'gemini':
                incomplete['candidates'][0]['finishReason'] = 'MAX_TOKENS'
                refused['candidates'][0]['finishReason'] = 'SAFETY'
            else:
                incomplete['choices'][0]['finish_reason'] = 'length'
                refused['choices'][0]['message']['refusal'] = 'synthetic refusal'
            with ApiTranslationSession(config, KEY) as session:
                for reply, code in ((incomplete, 'api_finish'), (refused, 'api_refusal'),
                                    (text_response_for(provider, []), 'api_envelope'),
                                    (text_response_for(provider, ' \n'), 'api_empty')):
                    with self.subTest(provider=provider, code=code), self.assertRaises(TranslationError) as caught:
                        session._parse(reply, native=True, native_batch=True, standard_native=True)
                    self.assertEqual(caught.exception.code, code)

    def test_standard_native_keeps_http_completeness_and_cancellation_checks(self):
        for options, code in (({'incomplete_http': True}, 'api_incomplete'),
                              ({'status': 429}, 'quota'), ({'reply': b'not outer JSON'}, 'api_json')):
            with self.subTest(options=options), mock_api(**options) as (config, records, _, _):
                with ApiTranslationSession(config, KEY) as session:
                    with self.assertRaises(TranslationError) as caught:
                        session.translate(PROMPT, [], native=True, native_batch=True, standard_native=True)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(records), 1)
        with mock_api() as (config, records, _, _):
            event = threading.Event()
            event.set()
            with ApiTranslationSession(config, KEY) as session:
                with self.assertRaises(TranslationError) as caught:
                    session.translate(PROMPT, [], cancel_event=event, native=True, standard_native=True)
            self.assertEqual(caught.exception.code, 'cancelled')
            self.assertEqual(records, [])

    def test_native_flow_preserves_engine_output_without_json_object_wrapper(self):
        for provider in ('deepseek', 'openai_compatible', 'openai', 'anthropic', 'gemini'):
            for batch, output in ((True, '[{"id":0,"output":"模拟译文 {v1}"}]'),
                                  (False, '模拟单段译文 {v1}')):
                reply = text_response_for(provider, output)
                with self.subTest(provider=provider, batch=batch), mock_api(provider, reply=reply) as (config, records, _, _):
                    with ApiTranslationSession(config, KEY) as session:
                        result = session.translate(PROMPT, [], native=True, native_batch=batch)
                    self.assertEqual(result['response'], output)
                    body = records[0]['body']
                    self.assertNotIn('response_format', body)
                    self.assertNotIn('text', body)
                    self.assertNotIn('responseMimeType', body.get('generationConfig', {}))

    def test_duplicate_response_fields_are_rejected_at_the_correct_layer_without_retry(self):
        for provider in ('deepseek', 'openai', 'anthropic', 'gemini'):
            original = json.dumps(response_for(provider)).encode()
            outer = b'{"duplicate":1,"duplicate":2,' + original[1:]
            with self.subTest(provider=provider, layer='outer'), mock_api(provider, reply=outer) as (config, records, _, _):
                with ApiTranslationSession(config, KEY) as session:
                    with self.assertRaises(TranslationError) as caught:
                        session.translate(PROMPT, [])
                self.assertEqual(caught.exception.code, 'api_json')
                self.assertEqual(caught.exception.details['stage'], 'outer_json')
                self.assertEqual(len(records), 1)
        for content, native_batch, generic in (
                ('{"response":"first","response":"last"}', False, False),
                ('[{"id":999,"id":0,"output":"译文"}]', True, False),
                ('{"translation":"first","translation":"last"}', False, True),
                ('{"response":"有效译文","terms":NaN}', False, False),
                ('{"response":"有效译文","terms":Infinity}', False, False),
                ('{"translation":-Infinity}', False, True)):
            reply = response_for('deepseek')
            reply['choices'][0]['message']['content'] = content
            with self.subTest(content=content), mock_api(reply=reply) as (config, records, _, _):
                with ApiTranslationSession(config, KEY) as session:
                    with self.assertRaises(TranslationError) as caught:
                        if generic:
                            session.request_json('只返回对象', PROMPT)
                        else:
                            session.translate(PROMPT, [], native_batch=native_batch)
                self.assertEqual(caught.exception.code, 'api_content_json')
                self.assertEqual(caught.exception.details['stage'], 'content_json')
                self.assertEqual(len(records), 1)

    def test_native_batch_accepts_direct_array_and_array_valued_envelope_for_all_protocols(self):
        outputs = [{'id': 0, 'output': '晶粒尺寸 <b1>5 μm</b1>。'}]
        for provider in ('deepseek', 'openai_compatible', 'openai', 'anthropic', 'gemini'):
            for content in (outputs, {'response': outputs, 'terms': []}):
                with self.subTest(provider=provider, wrapped=isinstance(content, dict)), \
                        mock_api(provider, reply=response_for(provider, content)) as (config, records, _, _):
                    with ApiTranslationSession(config, KEY) as session:
                        result = session.translate(PROMPT, [], native_batch=True)
                    self.assertEqual(json.loads(result['response']), outputs)
                    self.assertEqual(result['terms'], [])
                    self.assertTrue(result['usage'])
                    self.assertEqual(len(records), 1)

    def test_direct_batch_compatibility_cannot_leak_into_generic_or_single_requests(self):
        outputs = [{'id': 0, 'output': '有效文字'}]
        for content in (outputs, {'response': outputs}):
            with mock_api(reply=response_for('deepseek', content)) as (config, _, _, _):
                with ApiTranslationSession(config, KEY) as session:
                    with self.assertRaises(TranslationError) as caught:
                        session.translate(PROMPT, [])
                    self.assertEqual(caught.exception.code, 'api_envelope')
            if isinstance(content, list):
                with mock_api(reply=response_for('deepseek', content)) as (config, _, _, _):
                    with ApiTranslationSession(config, KEY) as session:
                        with self.assertRaises(TranslationError) as caught:
                            session.request_json('只返回对象', '合成输入')
                    self.assertEqual(caught.exception.code, 'api_envelope')

    def test_protocols_reuse_connection_without_history_and_preserve_prompt(self):
        for provider in ('deepseek', 'openai_compatible', 'openai', 'anthropic', 'gemini'):
            with self.subTest(provider=provider), mock_api(provider) as (config, records, _, _):
                glossary = [{'source': 'Grain size', 'target': '晶粒尺寸'}]
                with ApiTranslationSession(config, KEY) as session:
                    first = session.translate(PROMPT, glossary)
                    second = session.translate('第二批\n{"b2":"Text."}', [])
                self.assertEqual(first['response'], ENVELOPE['response'])
                self.assertEqual(first['terms'], ENVELOPE['terms'])
                self.assertTrue(second['usage'])
                self.assertEqual(len(records), 2)
                self.assertEqual(records[0]['peer'], records[1]['peer'])
                body = records[0]['body']
                headers = {key.lower(): value for key, value in records[0]['headers'].items()}
                self.assertNotIn('tools', body)
                self.assertNotIn('thinking', body)
                self.assertNotIn('previous_response_id', body)
                if provider == 'gemini':
                    self.assertEqual(records[0]['path'], '/v1/models/test-model:generateContent')
                    self.assertEqual(headers['x-goog-api-key'], KEY)
                    self.assertEqual(body['contents'], [{'role': 'user', 'parts': [{'text': PROMPT}]}])
                    system = body['systemInstruction']['parts'][0]['text']
                    self.assertEqual(body['generationConfig']['maxOutputTokens'], 2048)
                elif provider == 'anthropic':
                    self.assertEqual(records[0]['path'], '/v1/messages')
                    self.assertEqual(headers['x-api-key'], KEY)
                    self.assertEqual(headers['anthropic-version'], '2023-06-01')
                    self.assertEqual(body['messages'], [{'role': 'user', 'content': PROMPT}])
                    system = body['system']
                    self.assertEqual(body['max_tokens'], 2048)
                elif provider == 'openai':
                    self.assertEqual(records[0]['path'], '/v1/responses')
                    self.assertEqual(headers['authorization'], 'Bearer ' + KEY)
                    self.assertIs(body['store'], False)
                    self.assertEqual(body['input'], [{'role': 'user', 'content': PROMPT}])
                    system = body['instructions']
                    self.assertEqual(body['max_output_tokens'], 2048)
                else:
                    self.assertEqual(records[0]['path'], '/v1/chat/completions')
                    self.assertEqual(headers['authorization'], 'Bearer ' + KEY)
                    self.assertEqual(body['messages'][1], {'role': 'user', 'content': PROMPT})
                    self.assertEqual(len(body['messages']), 2)
                    system = body['messages'][0]['content']
                    self.assertEqual(body['max_tokens'], 2048)
                self.assertIn(json.dumps(glossary, ensure_ascii=False), system)
                second_body = json.dumps(records[1]['body'], ensure_ascii=False)
                self.assertNotIn('Grain size', second_body)
                self.assertNotIn(ENVELOPE['response'], second_body)

    def test_explicit_deepseek_thinking_is_sent_without_changing_prompt(self):
        for thinking in ('enabled', 'disabled'):
            with self.subTest(thinking=thinking), mock_api() as (config, records, _, _):
                with ApiTranslationSession({**config, 'thinking': thinking}, KEY) as session:
                    session.translate(PROMPT, [])
                self.assertEqual(records[0]['body']['thinking'], {'type': thinking})
                self.assertEqual(records[0]['body']['messages'][1]['content'], PROMPT)

    def test_chat_reasoning_uses_provider_protocol_and_default_sends_no_effort(self):
        for provider in ('openai', 'openai_compatible'):
            for effort in (None, 'low', 'medium', 'high', 'xhigh', 'max'):
                with self.subTest(provider=provider, effort=effort), mock_api(provider) as (config, records, _, _):
                    chosen = {**config, **({'reasoning_effort': effort} if effort else {})}
                    with ApiTranslationSession(chosen, KEY, channel='chat') as session:
                        session.request_json('本篇问答', '当前问题')
                    body = records[0]['body']
                    self.assertNotIn('thinking', body)
                    if provider == 'openai':
                        self.assertNotIn('reasoning_effort', body)
                        self.assertEqual(body.get('reasoning'), {'effort': effort} if effort else None)
                    else:
                        self.assertNotIn('reasoning', body)
                        self.assertEqual(body.get('reasoning_effort'), effort)

    def test_deepseek_three_efforts_are_sent_and_invalid_combinations_never_connect(self):
        for effort in ('low', 'high', 'max'):
            with mock_api() as (config, records, _, _):
                with ApiTranslationSession({**config, 'thinking': 'enabled', 'reasoning_effort': effort}, KEY) as session:
                    session.translate(PROMPT, [])
                self.assertEqual(records[0]['body']['reasoning_effort'], effort)
                self.assertEqual(records[0]['body']['thinking'], {'type': 'enabled'})
        for options in ({'reasoning_effort': 'low'}, {'thinking': 'disabled', 'reasoning_effort': 'high'},
                        {'provider': 'openai', 'thinking': 'enabled', 'reasoning_effort': 'high'},
                        {'thinking': 'enabled', 'reasoning_effort': 'medium'}):
            with self.assertRaises(TranslationError):
                ApiTranslationSession({**config, **options}, KEY)

    def test_thinking_is_not_invented_for_other_channels_and_invalid_snapshot_is_rejected(self):
        for channel in ('chat', 'overview', 'structure'):
            with self.subTest(channel=channel), mock_api() as (config, records, _, _):
                with ApiTranslationSession(config, KEY, channel=channel) as session:
                    session.request_json('保持原有规则。', PROMPT)
                self.assertNotIn('thinking', records[0]['body'])
        for provider, thinking in (('deepseek', None), ('deepseek', 'auto'), ('openai', 'disabled'),
                                   ('openai_compatible', 'disabled'), ('anthropic', 'enabled'), ('gemini', 'disabled')):
            config = {'provider': provider, 'base_url': 'https://example.invalid', 'model': 'test-model',
                      'max_output_tokens': 2048, 'thinking': thinking}
            with self.subTest(provider=provider, thinking=thinking), self.assertRaises(TranslationError):
                ApiTranslationSession(config, KEY)

    def test_http_errors_are_sanitized_and_never_retried(self):
        for status, code in ((401, 'authentication'), (403, 'authentication'), (429, 'quota'),
                             (500, 'api_server'), (400, 'api_request'), (302, 'api_request')):
            with self.subTest(status=status), mock_api(status=status, reply={'error': KEY}) as data:
                config, records, _, _ = data
                with ApiTranslationSession(config, KEY) as session:
                    with self.assertRaises(TranslationError) as error:
                        session.translate(PROMPT, [])
                self.assertEqual(error.exception.code, code)
                self.assertFalse(error.exception.retryable)
                self.assertNotIn(KEY, str(error.exception))
                self.assertEqual(len(records), 1)

    def test_incomplete_and_malformed_output_is_rejected_for_every_protocol(self):
        for provider in ('deepseek', 'openai', 'anthropic', 'gemini'):
            incomplete = response_for(provider)
            if provider == 'openai':
                incomplete['status'] = 'incomplete'
            elif provider == 'anthropic':
                incomplete['stop_reason'] = 'max_tokens'
            elif provider == 'gemini':
                incomplete['candidates'][0]['finishReason'] = 'MAX_TOKENS'
            else:
                incomplete['choices'][0]['finish_reason'] = 'length'
            for reply in (incomplete, {}, b'not JSON ' + KEY.encode()):
                with self.subTest(provider=provider, reply=reply), mock_api(provider, reply=reply) as data:
                    config, records, _, _ = data
                    with ApiTranslationSession(config, KEY) as session:
                        with self.assertRaises(TranslationError) as error:
                            session.translate(PROMPT, [])
                    self.assertNotIn(KEY, str(error.exception))
                    self.assertEqual(len(records), 1)

    def test_invalid_envelopes_and_empty_output_are_rejected(self):
        for envelope in ({'response': '', 'terms': []}, {'response': 1, 'terms': []},
                         {'terms': []}, 'raw string'):
            reply = response_for('deepseek')
            reply['choices'][0]['message']['content'] = json.dumps(envelope)
            with self.subTest(envelope=envelope), mock_api(reply=reply) as (config, _, _, _):
                with ApiTranslationSession(config, KEY) as session:
                    with self.assertRaises(TranslationError):
                        session.translate(PROMPT, [])

    def test_whitespace_only_provider_response_is_rejected_without_retry(self):
        reply = response_for('deepseek')
        reply['choices'][0]['message']['content'] = ' '
        reply['usage'] = {'completion_tokens': 1}
        for native_batch in (False, True):
            with self.subTest(native_batch=native_batch), mock_api(reply=reply) as (config, records, _, _):
                with ApiTranslationSession(config, KEY) as session:
                    with self.assertRaises(TranslationError) as caught:
                        session.translate(PROMPT, [], native_batch=native_batch)
                self.assertEqual(caught.exception.code, 'api_empty')
                self.assertEqual(len(records), 1)

    def test_bad_or_missing_terms_do_not_discard_valid_translation(self):
        envelopes = [{'response': '有效译文'}, *({'response': '有效译文', 'terms': terms}
                     for terms in ('bad', None, {'source': 'x'}, [None],
                                   [{'source': 'x', 'target': ''}]))]
        for envelope in envelopes:
            reply = response_for('deepseek')
            reply['choices'][0]['message']['content'] = json.dumps(envelope)
            with self.subTest(envelope=envelope), mock_api(reply=reply) as (config, _, _, _):
                with ApiTranslationSession(config, KEY) as session:
                    result = session.translate(PROMPT, [])
                self.assertEqual(result['response'], '有效译文')
                self.assertEqual(result['terms'], envelope.get('terms', []))
    def test_refusal_and_tools_are_rejected(self):
        for field in ('refusal', 'tool_calls'):
            reply = response_for('deepseek')
            reply['choices'][0]['message'][field] = ['not allowed']
            with mock_api(reply=reply) as (config, _, _, _):
                with ApiTranslationSession(config, KEY) as session:
                    with self.assertRaises(TranslationError):
                        session.translate(PROMPT, [])

    def test_cancel_before_request_sends_nothing(self):
        with mock_api() as (config, records, _, _):
            event = threading.Event()
            event.set()
            with ApiTranslationSession(config, KEY) as session:
                with self.assertRaises(TranslationError) as error:
                    session.translate(PROMPT, [], cancel_event=event)
            self.assertEqual(error.exception.code, 'cancelled')
            self.assertEqual(records, [])

    def test_cancel_interrupts_slow_body_and_closes_actual_socket(self):
        with mock_api(slow_body=True) as (config, records, started, disconnected):
            event = threading.Event()
            errors = []
            with ApiTranslationSession(config, KEY) as session:
                def translate():
                    try:
                        session.translate(PROMPT, [], cancel_event=event)
                    except TranslationError as exc:
                        errors.append(exc)
                thread = threading.Thread(target=translate)
                thread.start()
                self.assertTrue(started.wait(2))
                time.sleep(0.05)
                begin = time.monotonic()
                event.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic() - begin, 1)
                self.assertTrue(disconnected.wait(1))
            self.assertEqual(errors[0].code, 'cancelled')
            self.assertEqual(len(records), 1)

    def test_timeout_is_bounded_and_connection_failure_not_retried(self):
        with mock_api(delay=0.5) as (config, records, _, _):
            with ApiTranslationSession(config, KEY) as session:
                session.TIMEOUT_SECONDS = 0.1
                with self.assertRaises(TranslationError) as error:
                    session.translate(PROMPT, [])
            self.assertEqual(error.exception.code, 'timeout')
            self.assertEqual(len(records), 1)
        with mock_api(drop=True) as (config, records, _, _):
            with ApiTranslationSession(config, KEY) as session:
                with self.assertRaises(TranslationError):
                    session.translate(PROMPT, [])
            self.assertEqual(len(records), 1)

    def test_cancel_during_connect_does_not_send_late_request(self):
        with mock_api() as (config, records, _, _):
            entered = threading.Event()
            release = threading.Event()
            finished = threading.Event()
            event = threading.Event()
            errors = []
            original_connect = http.client.HTTPConnection.connect

            def delayed_connect(connection):
                entered.set()
                release.wait(2)
                original_connect(connection)
                finished.set()

            def translate(session):
                try:
                    session.translate(PROMPT, [], cancel_event=event)
                except TranslationError as exc:
                    errors.append(exc)

            with patch.object(http.client.HTTPConnection, 'connect', delayed_connect):
                with ApiTranslationSession(config, KEY) as session:
                    thread = threading.Thread(target=translate, args=(session,))
                    thread.start()
                    self.assertTrue(entered.wait(2))
                    event.set()
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                    release.set()
                    self.assertTrue(finished.wait(2))
            self.assertEqual(errors[0].code, 'cancelled')
            self.assertEqual(records, [])

    def test_valid_json_in_incomplete_http_body_is_rejected(self):
        with mock_api(incomplete_http=True) as (config, _, _, _):
            with ApiTranslationSession(config, KEY) as session:
                with self.assertRaises(TranslationError) as error:
                    session.translate(PROMPT, [])
            self.assertEqual(error.exception.code, 'api_incomplete')

    def test_response_size_and_usage_filter(self):
        with mock_api() as (config, _, _, _):
            with ApiTranslationSession(config, KEY) as session:
                session.MAX_RESPONSE_BYTES = 8
                with self.assertRaises(TranslationError) as error:
                    session.translate(PROMPT, [])
            self.assertEqual(error.exception.code, 'api_too_large')
        reply = response_for('deepseek')
        reply['usage'] = {'total_tokens': 5, 'untrusted_text': KEY, KEY: 1, 'nested': {'key': KEY}}
        with mock_api(reply=reply) as (config, _, _, _):
            with ApiTranslationSession(config, KEY) as session:
                self.assertEqual(session.translate(PROMPT, [])['usage'], {'total_tokens': 5})

    def test_reject_insecure_remote_urls_and_invalid_keys_without_connecting(self):
        config = {'provider': 'deepseek', 'base_url': 'https://example.invalid/v1',
                  'model': 'test', 'max_output_tokens': 2048}
        for url in ('http://example.invalid', 'https://user:pass@example.invalid',
                    'https://example.invalid?key=secret', 'file:///local'):
            with self.subTest(url=url), patch('http.client.HTTPSConnection') as connection:
                with self.assertRaises(TranslationError):
                    ApiTranslationSession({**config, 'base_url': url}, KEY)
                connection.assert_not_called()
        for key in ('', 'key\nheader', '中文'):
            with self.assertRaises(TranslationError) as error:
                ApiTranslationSession(config, key)
            self.assertNotIn('example.invalid', str(error.exception))

    def test_closed_session_cannot_send_and_clears_key(self):
        with mock_api() as (config, records, _, _):
            with ApiTranslationSession(config, KEY) as session:
                pass
            self.assertEqual(session._key, '')
            with self.assertRaises(TranslationError):
                session.translate(PROMPT, [])
            self.assertEqual(records, [])


if __name__ == '__main__':
    unittest.main()
