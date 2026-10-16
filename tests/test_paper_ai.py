"""Offline regression for per-paper context ownership and handoff."""
import contextlib
import json
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import codex_models
import codex_bridge
import paper_ai
from ezread import api_settings
RealCodexSession=paper_ai.CodexSession

PID='0123456789abcdef'


def resolve(model='',reasoning_effort=''):
    if model and model not in ('model-a','model-b'):
        raise ValueError('unavailable model')
    return {'model':model or 'model-a','reasoning_effort':reasoning_effort or 'medium'}


class FakeSession:
    next_id=0
    calls=[]
    def __init__(self,*_,**kwargs):
        self.temp=type('Temp',(),{'name':str(ROOT/'work')})()
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def close(self): self.calls.append(('close', {}))
    def rpc(self,method,params):
        self.calls.append((method,params))
        if method=='thread/start':
            FakeSession.next_id+=1
            return {'thread':{'id':f'00000000-0000-0000-0000-{FakeSession.next_id:012d}'}}
        return {}
    def turn(self,thread_id,prompt,config,schema,**_):
        self.calls.append(('turn',{'thread_id':thread_id,'prompt':prompt,'config':config}))
        if 'answer' in schema['properties']:
            return {'answer':'实验证据见所引段落。','citations':['p1-b1']}
        return {key:f'译文 {key}' for key in schema['required']}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.result = {'answer': '实验证据见所引段落。', 'citations': ['p1-b1']}

    def request_json(self, channel, config, api_key, system, prompt, *, cancel_event=None):
        self.calls.append((channel, dict(config), api_key, system, prompt))
        return self.result


class PdfProtocolProcess:
    """模拟 stdio app-server；任何请求只在内存中处理，不启动 CLI 或访问网络。"""
    def __init__(self, skills):
        self.skills = skills
        self.requests = []
        self.lines = queue.Queue()
        self.stdout = iter(self.lines.get, None)
        self.stdin = self
        self.returncode = None

    def write(self, line):
        request = json.loads(line)
        self.requests.append(request)
        method, params = request['method'], request.get('params', {})
        result = {}
        if method == 'skills/list':
            result = {'data': [{'cwd': params['cwds'][0], 'skills': self.skills, 'errors': []}]}
        elif method == 'thread/start':
            result = {'thread': {'id': 'pdf-thread'}}
        elif method == 'turn/start':
            result = {'turn': {'id': 'pdf-turn'}}
        if 'id' in request:
            self.lines.put(json.dumps({'id': request['id'], 'result': result}) + '\n')
        if method == 'turn/start':
            self.lines.put(json.dumps({'method': 'turn/completed', 'params': {
                'threadId': params['threadId'], 'turn': {'id': 'pdf-turn', 'status': 'completed',
                'items': [{'type': 'agentMessage', 'text': '{"response":"mock translation"}'}]}}}) + '\n')

    def flush(self): pass

    def close(self):
        self.returncode = 0
        self.lines.put(None)


class PaperAITests(unittest.TestCase):
    def setUp(self):
        work=ROOT/'work';work.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='paper-ai-',dir=work)
        self.root=Path(self.temp.name)
        self.library=self.root/'library';(self.library/PID).mkdir(parents=True)
        self.path=self.root/'library.sqlite3'
        with self.db() as con:
            con.execute('CREATE TABLE papers(id TEXT PRIMARY KEY)')
            con.execute('INSERT INTO papers VALUES(?)',(PID,))
            paper_ai.init(con)
        self.doc={'id':PID,'title':'Test paper','notes':'Measure force carefully.',
                  'blocks':[{'id':'p1-b1','page':1,'text':'We measured contact forces at 10 Hz.'}],
                  'translation':{'status':'idle'}}
        self.api = FakeAPI()
        api_settings.save_settings(self.root, {'provider': 'deepseek', 'api_key': 'test-only-key'})
        self.model_patch=patch.object(codex_models,'resolve_config',side_effect=resolve)
        self.session_patch=patch.object(paper_ai,'CodexSession',FakeSession)
        self.model_patch.start();self.session_patch.start()
        FakeSession.calls=[];FakeSession.next_id=0

    def tearDown(self):
        self.session_patch.stop();self.model_patch.stop();self.temp.cleanup()

    @contextlib.contextmanager
    def db(self):
        con=sqlite3.connect(self.path)
        try:
            with con: yield con
        finally: con.close()

    def ask(self, question='What was measured?', **kwargs):
        return paper_ai.ask(self.db, PID, self.doc, question, data_dir=self.root, api_client=self.api, **kwargs)

    def test_ask_reuses_client_and_preserves_history_across_model_switch(self):
        first = self.ask()
        second = self.ask('At what rate?')
        self.assertIsNone(first['thread_id'])
        self.assertEqual(first['generation'], second['generation'])
        self.assertEqual(len(self.api.calls), 2)
        self.assertFalse(FakeSession.calls)
        with self.db() as con:
            old = paper_ai.history(con, PID)
            self.assertEqual(len(old['messages']), 4)
            checked = api_settings.resolve_config(self.root, {'model': 'model-b'})
            result = paper_ai.switch_api_model(con, PID, checked, self.doc, self.library)
            self.assertEqual(result['generation'], 2)
        handoff = json.loads((self.library / PID / 'ai-handoff-2.json').read_text(encoding='utf-8'))
        self.assertEqual(handoff['user_notes'], 'Measure force carefully.')
        self.assertEqual(len(handoff['recent_local_history']), 4)
        third = self.ask('Does it report contact force?')
        self.assertEqual(third['generation'], 2)
        with self.db() as con:
            self.assertEqual(len(paper_ai.history(con, PID, 1)['messages']), 4)
            self.assertEqual(len(paper_ai.history(con, PID)['messages']), 2)

    def test_translation_thread_is_separate_and_selection_is_stateless(self):
        config={'model':'model-a','reasoning_effort':'medium'}
        saved=[]
        tid,result=paper_ai.translate_batch(self.db,PID,self.doc,self.doc['blocks'],config,None,
                                              on_thread=saved.append)
        self.assertEqual(saved,[tid])
        tid2,_=paper_ai.translate_batch(self.db,PID,self.doc,self.doc['blocks'],config,tid)
        self.assertEqual(tid,tid2)
        self.assertEqual(result,{'p1-b1':'译文 p1-b1'})
        with self.db() as con:
            self.assertFalse(paper_ai.history(con,PID)['messages'])
        with patch('selection_codex.translate',return_value='接触力'):
            self.assertEqual(codex_bridge.translate_selection('contact force',**config),'接触力')

    def test_invalid_citation_is_not_saved(self):
        self.api.result = {'answer': '不实引用', 'citations': ['another-paper']}
        with self.assertRaises(paper_ai.SessionError):
            self.ask()
        with self.db() as con:
            self.assertEqual(paper_ai.history(con, PID)['messages'], [])

    def test_native_pdf_prompt_is_forwarded_without_rebuilding_units(self):
        config = {'model': 'model-a', 'reasoning_effort': 'medium'}
        prompt = 'Upstream context and rules\n## Here is the input:\n\n[{"id":0,"input":"Source {v1}"}]'
        response = '[{"id":0,"output":"译文 {v1}"}]'
        seen = []
        def translate(session, tid, received, options, schema, **kwargs):
            seen.append((received, options, schema))
            return {'response': response}
        with patch.object(FakeSession, 'turn', translate):
            tid, value = paper_ai.translate_pdf_request(prompt, config)
            tid2, _ = paper_ai.translate_pdf_request(prompt, config, tid)
        self.assertEqual(tid, tid2)
        self.assertEqual(value, response)
        self.assertEqual(seen[0][0], prompt)
        self.assertEqual(seen[0][1], config)
        self.assertEqual(seen[0][2]['required'], ['response'])
        with self.db() as con:
            self.assertFalse(paper_ai.history(con, PID)['messages'])

    def test_native_pdf_empty_response_is_not_accepted(self):
        with patch.object(FakeSession, 'turn', return_value={'response': ' '}):
            with self.assertRaises(paper_ai.SessionError):
                paper_ai.translate_pdf_request('Upstream prompt', {'model': 'model-a', 'reasoning_effort': 'medium'})

    def test_pdf_task_reuses_one_transport_and_resets_each_turn_deadline(self):
        config = {'model': 'model-a', 'reasoning_effort': 'medium'}
        with patch.object(paper_ai, 'CodexSession', wraps=FakeSession) as created, \
             patch.object(FakeSession, 'turn', return_value={'response': '译文'}):
            with paper_ai.PdfTranslationSession() as transport:
                tid, _ = paper_ai.translate_pdf_request('first', config, 'saved-thread', transport=transport)
                transport.session.deadline = 0
                self.assertEqual(paper_ai.translate_pdf_request('second', config, tid, transport=transport)[0], tid)
                self.assertGreater(transport.session.deadline, time.monotonic() + 400)
                self.assertEqual(created.call_count, 1)
                created.assert_called_once_with(pdf_translation=True)
            self.assertEqual([method for method, _ in FakeSession.calls], ['thread/resume', 'close'])

    def test_pdf_transport_is_lazy_and_cancelled_before_start_uses_no_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(paper_ai, 'CodexSession') as created:
            with paper_ai.PdfTranslationSession():
                pass  # 全缓存任务。
            with paper_ai.PdfTranslationSession() as transport:
                with self.assertRaises(paper_ai.SessionError) as caught:
                    paper_ai.translate_pdf_request('unused', {}, cancel_event=cancel, transport=transport)
                self.assertEqual(caught.exception.code, 'cancelled')
            created.assert_not_called()

    def test_pdf_transport_closes_on_failure_and_rejects_cross_task_config(self):
        config = {'model': 'model-a', 'reasoning_effort': 'medium'}
        with patch.object(FakeSession, 'turn', return_value={'response': '译文'}):
            with self.assertRaises(paper_ai.SessionError), paper_ai.PdfTranslationSession() as transport:
                tid, _ = paper_ai.translate_pdf_request('first', config, transport=transport)
                paper_ai.translate_pdf_request('second', {**config, 'model': 'model-b'}, tid, transport=transport)
        self.assertEqual(sum(method == 'close' for method, _ in FakeSession.calls), 1)
        with patch.object(FakeSession, 'turn', side_effect=paper_ai.SessionError('offline')):
            with self.assertRaises(paper_ai.SessionError), paper_ai.PdfTranslationSession() as transport:
                paper_ai.translate_pdf_request('third', config, transport=transport)
        self.assertEqual(sum(method == 'close' for method, _ in FakeSession.calls), 2)

    def test_pdf_process_isolation_covers_discovered_skills_and_preserves_protocol(self):
        home = self.root / 'codex-home'
        home.mkdir()
        settings = home / 'config.toml'
        original = ('[features]\nmemories=true\n[agents]\nenabled=true\n'
                    '[mcp_servers.sample]\ncommand="unused"\n[plugins."sample@local"]\nenabled=true\n')
        settings.write_text(original, encoding='utf-8')
        paths = [str(home / 'skills/builtin/SKILL.md'),
                 str(self.root / '.agents/skills/user/SKILL.md'),
                 str(self.root / 'plugins/sample/SKILL.md')]
        config = {'model': 'model-a', 'reasoning_effort': 'medium'}
        prompts = ['Rules\n## Here is the input:\n\n[{"id":0,"input":"Force {v1}","layout_label":"text"}]',
                   'Rules\nNow translate the following text:\n\n<b1>Force</b1>']
        for saved_thread in (None, 'existing-pdf-thread'):
            with self.subTest(resume=bool(saved_thread)):
                process = PdfProtocolProcess([{'path': path, 'enabled': True} for path in paths])
                with patch.object(paper_ai, 'CodexSession', RealCodexSession), \
                     patch.object(codex_bridge, '_cli', return_value='fake-codex'), \
                     patch.object(codex_bridge, '_environment', return_value={'CODEX_HOME': str(home)}), \
                     patch.object(codex_bridge, 'spawn', return_value=process) as spawn, \
                     patch.object(codex_bridge, '_stop', side_effect=lambda p: p.close()):
                    with paper_ai.PdfTranslationSession() as transport:
                        tid, value = paper_ai.translate_pdf_request(prompts[0], config, saved_thread, transport=transport)
                        tid2, _ = paper_ai.translate_pdf_request(prompts[1], config, tid, transport=transport)
                self.assertEqual(tid, tid2)
                self.assertEqual(value, 'mock translation')
                spawn.assert_called_once()
                command = spawn.call_args.args[0]
                for setting in ('features.memories=false', 'memories.use_memories=false',
                                'memories.generate_memories=false', 'agents.enabled=false',
                                'project_doc_max_bytes=0', 'mcp_servers.sample.enabled=false',
                                'plugins.sample@local.enabled=false', 'forced_login_method="chatgpt"'):
                    self.assertIn(setting, command)
                methods = [r['method'] for r in process.requests]
                method = 'thread/resume' if saved_thread else 'thread/start'
                self.assertEqual(methods, ['initialize', 'initialized', 'skills/list', method, 'turn/start', 'turn/start'])
                started = next(r['params'] for r in process.requests if r['method'] == method)
                self.assertEqual(started['developerInstructions'], '')
                self.assertFalse(started['config']['agents']['enabled'])
                self.assertFalse(started['config']['memories']['use_memories'])
                self.assertEqual(started['config']['project_doc_max_bytes'], 0)
                self.assertEqual(started['config']['skills']['config'],
                                 [{'path': path, 'enabled': False} for path in sorted(paths)])
                self.assertNotIn('block_id', started['baseInstructions'])
                self.assertNotIn('用户笔记', started['baseInstructions'])
                self.assertNotIn(paper_ai.BASE, started['baseInstructions'])
                self.assertIn('不可信数据', started['baseInstructions'])
                self.assertIn('response', started['baseInstructions'])
                self.assertNotIn('history', started)
                if saved_thread:
                    self.assertEqual(started['threadId'], saved_thread)
                    self.assertTrue(started['excludeTurns'])
                else:
                    self.assertFalse(started['ephemeral'])
                    self.assertEqual(started['config']['model_auto_compact_token_limit'], 100000)
                turns = [r['params'] for r in process.requests if r['method'] == 'turn/start']
                self.assertEqual([t['input'][0]['text'] for t in turns], prompts)
                self.assertEqual([t['threadId'] for t in turns], [tid, tid])
                self.assertTrue(all(t['model'] == config['model'] and t['effort'] == config['reasoning_effort'] for t in turns))
                self.assertEqual(settings.read_text(encoding='utf-8'), original)
                self.assertEqual(process.returncode, 0)

    def test_pdf_incomplete_skill_discovery_stops_before_any_turn(self):
        for result in ({}, {'data': []}, {'data': [{'skills': [], 'errors': ['unreadable']}]},
                       {'data': [{'skills': [{'name': 'no-path'}], 'errors': []}]}):
            with self.subTest(result=result):
                process = PdfProtocolProcess([])
                def rpc(session, method, params):
                    return result if method == 'skills/list' else {}
                with patch.object(codex_bridge, '_cli', return_value='fake-codex'), \
                     patch('selection_codex.isolated_overrides', return_value=[]), \
                     patch.object(codex_bridge, 'spawn', return_value=process), \
                     patch.object(codex_bridge, '_stop', side_effect=lambda p: p.close()), \
                     patch.object(RealCodexSession, 'rpc', rpc):
                    with self.assertRaises(paper_ai.SessionError):
                        RealCodexSession(pdf_translation=True)
                self.assertFalse(any(r['method'] == 'turn/start' for r in process.requests))
                self.assertEqual(process.returncode, 0)

    def test_regular_session_does_not_enable_pdf_isolation(self):
        process = PdfProtocolProcess([])
        with patch.object(codex_bridge, '_cli', return_value='fake-codex'), \
             patch('selection_codex.isolated_overrides') as isolate, \
             patch.object(codex_bridge, 'spawn', return_value=process) as spawn, \
             patch.object(codex_bridge, '_stop', side_effect=lambda p: p.close()):
            with RealCodexSession() as session:
                paper_ai._start_or_resume(session, None, {'model': 'model-a'}, paper_ai.BASE, '{}')
        isolate.assert_not_called()
        self.assertNotIn('agents.enabled=false', spawn.call_args.args[0])
        self.assertNotIn('skills/list', [r['method'] for r in process.requests])
        started = next(r['params'] for r in process.requests if r['method'] == 'thread/start')
        self.assertEqual(started['baseInstructions'], paper_ai.BASE)
        self.assertNotIn('developerInstructions', started)
        self.assertNotIn('skills', started['config'])

    def test_regular_translation_does_not_use_pdf_boundary_metadata(self):
        config={'model':'model-a','reasoning_effort':'medium'}
        block={**self.doc['blocks'][0],
               'source_context':{'after':{'page':2,'text':'Unrelated PDF boundary metadata.'}}}
        paper_ai.translate_batch(self.db,PID,self.doc,[block],config,None)
        sent=json.loads(next(params['prompt'] for method,params in FakeSession.calls
                             if method=='turn').split('\n',1)[1])['blocks'][0]
        self.assertNotIn('source_context',sent)
        self.assertNotIn('placeholders',sent)

    def test_app_server_completed_turn_payload_is_parsed(self):
        session=RealCodexSession.__new__(RealCodexSession)
        session.deadline=time.monotonic()+1
        session.events=queue.Queue()
        session.rpc=lambda *_: {}
        session.events.put({'method':'turn/completed','params':{'threadId':'test-thread',
            'turn':{'status':'completed','items':[{'type':'agentMessage',
                'text':'{"answer":"结论","citations":["p1-b1"]}'}]}}})
        answer=RealCodexSession.turn(session,'test-thread','question',
            {'model':'model-a','reasoning_effort':'medium'},paper_ai.ANSWER_SCHEMA)
        self.assertEqual(answer['citations'],['p1-b1'])

    def test_stream_uses_only_matching_turn_and_readable_reasoning_summary(self):
        session=RealCodexSession.__new__(RealCodexSession)
        session.deadline=time.monotonic()+1
        session.events=queue.Queue()
        session.rpc=lambda *_: {'turn':{'id':'turn-a'}}
        raw=json.dumps({'answer':'**结论**\n接触力为 10 N。','citations':['p1-b1']},ensure_ascii=False)
        def put(method, **params):
            session.events.put({'method':method,'params':{'threadId':'thread-a','turnId':'turn-a',**params}})
        put('item/reasoning/summaryTextDelta',itemId='reason',summaryIndex=0,delta='检查实验段落。')
        put('item/reasoning/textDelta',itemId='reason',delta='raw reasoning must not be exposed')
        put('item/reasoning/summaryTextDelta',turnId='another-turn',delta='wrong turn')
        put('item/agentMessage/delta',itemId='answer',delta=raw[:16])
        put('item/agentMessage/delta',itemId='answer',delta=raw[16:])
        put('turn/completed',turn={'id':'turn-a','status':'completed','items':[{'type':'agentMessage','text':raw}]})
        events=[]
        answer=session.turn('thread-a','question',{'model':'model-a','reasoning_effort':'medium'},
                            paper_ai.ANSWER_SCHEMA,on_progress=events.append)
        self.assertEqual(answer['answer'],'**结论**\n接触力为 10 N。')
        self.assertEqual([e['text'] for e in events if e['type']=='reasoning'],['检查实验段落。'])
        self.assertEqual([e['text'] for e in events if e['type']=='answer'][-1],answer['answer'])
        self.assertNotIn('raw reasoning',str(events))

    def test_partial_answer_decodes_escapes_and_split_surrogate_without_json(self):
        self.assertEqual(paper_ai.answer_preview('{"answer":"第一行\\n第二行\\u4e2d\\u6587\\uD83D'), '第一行\n第二行中文')
        self.assertEqual(paper_ai.answer_preview('{"citations":[],"answer":"quote \\" and \\uD83D\\uDE00"}'), 'quote " and 😀')
        self.assertEqual(paper_ai.answer_preview('{"citations":["p1-b1"'), '')

    def test_legacy_reasoning_is_preserved_but_not_sent_to_api(self):
        with self.db() as con:
            paper_ai.add_message(con, PID, 1, 'assistant', 'reasoning', 'private-old-reasoning')
        events = []
        self.ask(on_progress=events.append)
        with self.db() as con:
            self.assertEqual(paper_ai.history(con, PID, 1)['messages'][0]['text'], 'private-old-reasoning')
        self.assertNotIn('private-old-reasoning', self.api.calls[0][-1])
        self.assertTrue(any(e['type'] == 'generation' for e in events))

    def test_item_completed_can_supply_answer_when_turn_items_are_empty(self):
        session=RealCodexSession.__new__(RealCodexSession)
        session.deadline=time.monotonic()+1
        session.events=queue.Queue()
        session.rpc=lambda *_: {'turn':{'id':'turn-a'}}
        for method,values in [('item/completed',{'item':{'type':'agentMessage','id':'a','text':'{"answer":"结论","citations":[]}'}}),
                             ('turn/completed',{'turn':{'id':'turn-a','status':'completed','items':[]}})]:
            session.events.put({'method':method,'params':{'threadId':'thread-a','turnId':'turn-a',**values}})
        self.assertEqual(session.turn('thread-a','q',{'model':'model-a','reasoning_effort':'medium'},paper_ai.ANSWER_SCHEMA)['answer'],'结论')

    def test_app_server_error_notification_is_reported(self):
        session=RealCodexSession.__new__(RealCodexSession)
        session.deadline=time.monotonic()+1
        session.events=queue.Queue()
        session.rpc=lambda *_: {'turn':{'id':'test-turn'}}
        session.events.put({'method':'error','params':{'threadId':'test-thread',
            'turnId':'test-turn','willRetry':False,
            'error':{'message':'rate limit reached'}}})
        with self.assertRaises(paper_ai.SessionError) as raised:
            RealCodexSession.turn(session,'test-thread','question',
                {'model':'model-a','reasoning_effort':'medium'},paper_ai.ANSWER_SCHEMA)
        self.assertEqual(raised.exception.code,'quota')

    def test_app_server_failed_turn_is_reported(self):
        session=RealCodexSession.__new__(RealCodexSession)
        session.deadline=time.monotonic()+1
        session.events=queue.Queue()
        session.rpc=lambda *_: {'turn':{'id':'test-turn'}}
        session.events.put({'method':'turn/completed','params':{'threadId':'test-thread',
            'turn':{'id':'test-turn','status':'failed',
                    'error':{'message':'connection timed out'},'items':[]}}})
        with self.assertRaises(paper_ai.SessionError) as raised:
            RealCodexSession.turn(session,'test-thread','question',
                {'model':'model-a','reasoning_effort':'medium'},paper_ai.ANSWER_SCHEMA)
        self.assertEqual(raised.exception.code,'network')


if __name__=='__main__': unittest.main()
