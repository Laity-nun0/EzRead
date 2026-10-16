"""用本地假模型检查真实 Codex CLI 的 PDF 通道；不使用账号、论文或远端模型。"""
from __future__ import annotations

import gzip
import hashlib
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_bridge
import paper_ai


class OfflineModel(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != '/v1/responses':
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers['Content-Length']))
        if self.headers.get('Content-Encoding') == 'gzip':
            raw = gzip.decompress(raw)
        request = json.loads(raw)
        self.requests.append(request)
        text = json.dumps({'response': 'offline translated text'})
        item = {'id': 'msg_offline', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
        response = {'id': 'resp_offline', 'object': 'response', 'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
                              'input_tokens_details': {'cached_tokens': 0},
                              'output_tokens_details': {'reasoning_tokens': 0}}}
        events = [
            {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': {**item, 'status': 'in_progress', 'content': []}},
            {'type': 'response.content_part.added', 'item_id': item['id'], 'output_index': 0,
             'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
            {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0,
             'content_index': 0, 'delta': text},
            {'type': 'response.output_text.done', 'item_id': item['id'], 'output_index': 0,
             'content_index': 0, 'text': text},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': response},
        ]
        body = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'work/pdf-channel-verification')
    args = parser.parse_args()
    cli = codex_bridge._cli()
    if not cli:
        raise SystemExit('未安装 Codex CLI，未运行验证。')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    OfflineModel.requests = []
    model = ThreadingHTTPServer(('127.0.0.1', 0), OfflineModel)
    worker = threading.Thread(target=model.serve_forever, daemon=True)
    worker.start()
    real_spawn = codex_bridge.spawn
    spawned, protocol = [], []
    real_rpc = paper_ai.CodexSession.rpc
    try:
        with tempfile.TemporaryDirectory(prefix='offline-cli-', dir=output) as folder:
            root = Path(folder)
            home = root / '.codex'
            home.mkdir()
            settings = home / 'config.toml'
            settings.write_text('developer_instructions="UNRELATED_DEVELOPER_SENTINEL"\n'
                                '[memories]\nuse_memories=true\ngenerate_memories=false\n'
                                '[agents]\nenabled=true\n', encoding='utf-8')
            (home / 'AGENTS.md').write_text('UNRELATED_AGENTS_SENTINEL', encoding='utf-8')
            (home / 'memories').mkdir()
            (home / 'memories/memory_summary.md').write_text('UNRELATED_MEMORY_SENTINEL', encoding='utf-8')
            for name, path in [('local', home / 'skills/local/SKILL.md'),
                               ('user', root / '.agents/skills/user/SKILL.md')]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'---\nname: offline-{name}\ndescription: UNRELATED_SKILL_SENTINEL\n---\nOffline fixture.\n', encoding='utf-8')
            before = hashlib.sha256(settings.read_bytes()).hexdigest()
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(('CODEX_', 'OPENAI_'))}
            env.update(CODEX_HOME=str(home), HOME=str(root), USERPROFILE=str(root),
                       HTTP_PROXY='http://127.0.0.1:1', HTTPS_PROXY='http://127.0.0.1:1',
                       ALL_PROXY='http://127.0.0.1:1', NO_PROXY='127.0.0.1,localhost')

            def spawn(command, **kwargs):
                # 仅此验证替换供应商；生产通道仍强制 ChatGPT，不提供 API 回退。
                provider = ('{name="Offline fixture",base_url="http://127.0.0.1:'
                            + str(model.server_port) + '/v1",wire_api="responses",'
                            'requires_openai_auth=false,supports_websockets=false,'
                            'request_max_retries=0,stream_max_retries=0}')
                command = [*command, '-c', 'model_provider="offline"', '-c', 'forced_login_method="api"',
                           '-c', 'model_providers.offline=' + provider,
                           '-c', 'analytics.enabled=false']
                process = real_spawn(command, **kwargs)
                spawned.append(process)
                return process

            def rpc(session, method, params):
                session.deadline = min(session.deadline, time.monotonic() + 30)
                result = real_rpc(session, method, params)
                protocol.append({'method': method, 'params': params, 'result': result})
                return result

            config = {'model': 'gpt-5.6-sol', 'reasoning_effort': 'medium'}
            prompts = ['Rules\n## Here is the input:\n\n[{"id":0,"input":"Synthetic first {v1}","layout_label":"text"}]',
                       'Rules\nNow translate the following text:\n\n<b1>Synthetic second</b1>',
                       'Rules\nNow translate the following text:\n\nSynthetic resumed']
            with patch.object(codex_bridge, '_environment', return_value=env), \
                 patch.object(codex_bridge, 'spawn', side_effect=spawn), \
                 patch.object(paper_ai.CodexSession, 'rpc', rpc):
                with paper_ai.PdfTranslationSession() as transport:
                    tid, first = paper_ai.translate_pdf_request(prompts[0], config, transport=transport)
                    tid2, second = paper_ai.translate_pdf_request(prompts[1], config, tid, transport=transport)
                    assert tid == tid2 and len(spawned) == 1
                with paper_ai.PdfTranslationSession() as transport:
                    tid3, third = paper_ai.translate_pdf_request(prompts[2], config, tid, transport=transport)
                    assert tid3 == tid
            assert first == second == third == 'offline translated text'
            assert len(OfflineModel.requests) == 3
            skill_paths = {s['path'] for p in protocol if p['method'] == 'skills/list'
                           for entry in p['result']['data'] for s in entry['skills']}
            assert any('local' in p for p in skill_paths), '合成技能未被发现，验证无效'
            counts = []
            for index, request in enumerate(OfflineModel.requests):
                visible = json.dumps(request, ensure_ascii=False)
                for unwanted in ('UNRELATED_MEMORY_SENTINEL', 'UNRELATED_SKILL_SENTINEL',
                                 'UNRELATED_DEVELOPER_SENTINEL', '<skills_instructions>', 'Available skills', '## Memory',
                                 'primary agent in a team', '<multi_agent_mode>', 'block_id'):
                    assert unwanted not in visible, (f'第 {index + 1} 轮仍含无关上下文：{unwanted} '
                                                     + str(sorted(set(re.findall(r'UNRELATED_[A-Z_]+', visible)))))
                texts = [c.get('text', '') for item in request.get('input', [])
                         for c in item.get('content', []) if isinstance(c, dict)]
                assert prompts[index] in texts, '上游提示词发生变化'
                if index:
                    assert prompts[0] in texts, '已有对话历史被清空'
                counts.append(len(visible))
            assert hashlib.sha256(settings.read_bytes()).hexdigest() == before
            assert all(p.poll() is not None for p in spawned), '验证子进程未退出'
            report = {'passed': True, 'real_model_calls': 0, 'mock_responses': 3,
                      'discovered_skills': len(skill_paths), 'processes': len(spawned),
                      'same_thread_resumed': True, 'prompts_unchanged': True,
                      'unrelated_context_absent': True, 'fixture_config_unchanged': True,
                      'user_agents_still_present': any('UNRELATED_AGENTS_SENTINEL' in json.dumps(r)
                                                      for r in OfflineModel.requests),
                      'request_chars': counts}
            (output / 'offline-cli-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
    finally:
        for process in spawned:
            if process.poll() is None:
                codex_bridge._stop(process)
        model.shutdown()
        model.server_close()


if __name__ == '__main__':
    main()
