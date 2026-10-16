"""隔离验证永久删除或手删资源目录后同 PDF 重导，不调用模型或 Codex。"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write_fixture(path):
    """生成带可提取英文和有效交叉引用表的单页 PDF，无额外绘图库。"""
    content = (b'BT /F1 20 Tf 60 730 Td (Synthetic import lifecycle paper) Tj '
               b'/F1 12 Tf 0 -40 Td (A synthetic document for deletion and import verification.) Tj '
               b'0 -22 Td (No private literature or model requests are involved.) Tj ET')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] '
               b'/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream']
    raw = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(f'{number} 0 obj\n'.encode() + value + b'\nendobj\n')
    xref = len(raw)
    raw.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        raw.extend(f'{offset:010d} 00000 n \n'.encode())
    raw.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.write_bytes(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--orphan', action='store_true', help='只移除隔离资源目录，验证孤儿记录重导清理。')
    args = parser.parse_args()
    mode = 'orphan-reimport' if args.orphan else 'import-delete'
    output = (ROOT / (args.output or f'work/integration-repair-20261016/browser-{mode}')).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (ROOT / 'work').mkdir(exist_ok=True)
    evidence = {'passed': False, 'mode': mode, 'realModelCalls': 0, 'realLibraryWrites': 0, 'forbidden': []}
    runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    node = os.environ.get('EZREAD_NODE') or shutil.which('node') or str(runtime / 'bin/node.exe')
    with tempfile.TemporaryDirectory(prefix='import-delete-', dir=ROOT / 'work', ignore_cleanup_errors=True) as temp:
        data = Path(temp)
        # 仅改变本次验证进程的路径；导入入口模块前就隔离数据目录。
        with patch.dict(os.environ, {'EZREAD_DATA_DIR': str(data)}):
            import server
        import codex_models
        import paper_ai
        from ezread import api_settings, translation_api
        server.DATA, server.LIBRARY = data, data / 'library'
        server.init_db()
        assert server.DATA != ROOT / 'data'
        fixture = data / 'synthetic-import.pdf'
        write_fixture(fixture)
        expected_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
        scheduled = []

        def forbidden(*_args, **_kwargs):
            evidence['forbidden'].append('model, Codex or outbound metadata access')
            raise AssertionError('隔离删除/重导验收禁止模型、Codex 与联网元数据查询')

        def counts(pid):
            with server.db() as con:
                return {table: con.execute(f'SELECT COUNT(*) FROM {table} WHERE {column}=?', (pid,)).fetchone()[0]
                        for table, column in (('papers', 'id'), ('paper_ai', 'paper_id'),
                                              ('paper_ai_generations', 'paper_id'), ('paper_ai_messages', 'paper_id'))}

        with ExitStack() as guards:
            for target, name in ((server, 'codex_status'), (codex_models, 'get'),
                                 (codex_models, 'resolve_config'), (paper_ai, 'CodexSession'),
                                 (server.AI_API, 'request_json'), (translation_api.ApiTranslationSession, '_send_json')):
                guards.enter_context(patch.object(target, name, side_effect=forbidden))
            guards.enter_context(patch('ezread.publication._read', side_effect=forbidden))
            guards.enter_context(patch.object(server, 'schedule_structure', side_effect=scheduled.append))
            # 不启动任何任务 worker；仅进行真实提取和正常 HTTP/UI 操作。
            old = server.import_pdf(fixture.read_bytes(), fixture.name)
            old_id = old['id']
            server.update_doc(old_id, lambda doc: doc.update(notes='Synthetic previous private note',
                              summary='Synthetic old summary', tags=['old-tag'],
                              translation={'status': 'completed', 'text': 'Synthetic old translation'}))
            with server.db() as con:
                con.execute('UPDATE paper_ai_generations SET model=?,reasoning_effort=?,main_thread_id=?,translation_thread_id=?,handoff=? WHERE paper_id=?',
                            ('fixture-old-codex', 'high', 'fixture-old-thread', 'fixture-old-translation-thread',
                             json.dumps({'fixture': 'Synthetic old handoff'}), old_id))
                paper_ai.add_message(con, old_id, 1, 'assistant', 'answer', 'Synthetic previous Codex answer')
            clipboard = data / 'clipboard' / old_id
            clipboard.mkdir(parents=True)
            (clipboard / 'synthetic-copy.pdf').write_bytes(fixture.read_bytes())
            snapshot_path = data / 'desktop-close-drafts.json'
            snapshot_path.write_text(json.dumps({'values': {f'ezread-reader-notes:{old_id}': 'old snapshot',
                                                           'ezread-sort': 'recent'}}), encoding='utf-8')
            api_settings.save_settings(data, {'provider': 'deepseek', 'base_url': 'http://127.0.0.1:9/v1',
                                             'model': 'fixture-api-default', 'api_key': 'fixture-local-only'})
            if args.orphan:
                orphan_resources = (server.LIBRARY / old_id).resolve()
                assert orphan_resources.is_relative_to(data.resolve()) and orphan_resources.parent == server.LIBRARY.resolve()
                shutil.rmtree(orphan_resources)

            class Handler(server.Handler):
                def do_GET(self):
                    if self.path == '/__test/import-delete-state':
                        keys = list(json.loads(snapshot_path.read_text(encoding='utf-8'))['values'])
                        self.send_json({'oldCounts': counts(old_id), 'oldResourcesExist': (server.LIBRARY / old_id).exists(),
                                        'oldClipboardExists': clipboard.exists(), 'snapshotKeys': keys,
                                        'queued': len(server.QUEUED), 'structureQueued': len(server.STRUCTURE_QUEUED)})
                        return
                    super().do_GET()

            listener = server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.PORT = listener.server_port
            thread = threading.Thread(target=listener.serve_forever, daemon=True)
            environment = {**os.environ, 'EZREAD_TEST_URL': f'http://127.0.0.1:{server.PORT}',
                           'EZREAD_TEST_OUTPUT': str(output), 'EZREAD_TEST_OLD_ID': old_id,
                           'EZREAD_TEST_PDF': str(fixture), 'EZREAD_TEST_PDF_HASH': expected_hash,
                           'EZREAD_TEST_ORPHAN': '1' if args.orphan else '0'}
            environment.setdefault('EZREAD_PLAYWRIGHT', str(runtime / 'node_modules/playwright'))
            try:
                thread.start()
                result = subprocess.run([node, str(ROOT / 'tests/browser_import_delete.cjs')],
                                        env=environment, cwd=ROOT, timeout=90)
                evidence['browserReturncode'] = result.returncode
                if result.returncode:
                    raise RuntimeError(f'浏览器验收失败，退出码 {result.returncode}；详见 browser-result.json')
                browser = json.loads((output / 'browser-result.json').read_text(encoding='utf-8'))
                new_id = browser['newId']
                evidence.update(oldId=old_id, newId=new_id, oldCounts=counts(old_id), newCounts=counts(new_id),
                                structureSchedulingSuppressed=scheduled, fixtureSHA256=expected_hash)
                assert evidence['oldCounts'] == dict.fromkeys(evidence['oldCounts'], 0)
                assert evidence['newCounts'] == {'papers': 1, 'paper_ai': 1, 'paper_ai_generations': 1, 'paper_ai_messages': 0}
                new = server.get_doc(new_id)
                assert new['notes'] == '' and new['summary'] == '' and new['tags'] == []
                assert new['translation']['status'] == 'idle'
                with server.db() as con:
                    current = paper_ai.current(con, new_id)
                assert all(current[key] is None for key in ('model', 'reasoning_effort', 'main_thread_id', 'translation_thread_id'))
                assert json.loads(current['handoff']) == {}
                evidence['newAIConfig'] = current
                assert hashlib.sha256((server.LIBRARY / new_id / 'original.pdf').read_bytes()).hexdigest() == expected_hash
                assert not evidence['forbidden'] and not server.QUEUED and not server.STRUCTURE_QUEUED
                evidence['passed'] = True
                print(f'{mode} 浏览器验收通过；模型/Codex 调用 0，真实文献库写入 0。')
            except Exception as error:
                evidence['error'] = str(error)
                raise
            finally:
                listener.shutdown()
                listener.server_close()
                thread.join(timeout=3)
                (output / 'result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
