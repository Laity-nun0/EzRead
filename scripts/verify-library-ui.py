"""用独立合成库验证文献库 UI；--serve-only 可供人工浏览，禁止真实模型调用。"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
import codex_models
import paper_ai
import pypdfium2 as pdfium
from PIL import Image
from ezread import ai_api, translation_api

LATEST_ID = '0000000000000007'


def seed_library(data):
    server.DATA, server.LIBRARY = data, data / 'library'
    server.init_db()
    # 复用已公开的项目展示图，只裁切其中的科学图片作为测试封面。
    source = Image.open(ROOT / 'docs/images/library.png')
    boxes = [(300, 235, 490, 408), (750, 240, 945, 390), (1205, 310, 1400, 430),
             (1655, 310, 1848, 450), (2110, 280, 2300, 470)]
    sources = [('ACS Nano', 'journal'), ('ACTA MECHANICA SINICA', 'journal'),
               ('Synthetic Research Journal', 'journal'), ('Synthetic Robotics Conference', 'conference'),
               ('arXiv', 'preprint')]
    for index in range(45):
        pid = f'{index:016x}'
        folder = server.LIBRARY / pid
        folder.mkdir(parents=True)
        image = source.crop(boxes[index % len(boxes)])
        image.save(folder / 'cover.png')
        with pdfium.PdfDocument.new() as document:
            for _ in range(8):
                document.new_page(width=600, height=800).close()
            document.save(folder / 'original.pdf')
        journal, kind = sources[index % len(sources)]
        title = ['Microstructure-guided design of damage-tolerant materials',
                 'An integrated tactile sensing system for soft robotic grasping, learning-based object recognition and force-controlled manipulation',
                 'Coupled interface mechanics and fatigue crack growth in layered metallic composites',
                 'Adaptive robotic manipulation with multiscale contact feedback',
                 'Processing pathways for spatially ordered aluminium alloys'][index % 5]
        doc = {'id': pid, 'hash': f'synthetic-library-ui-{index}', 'title': f'{title} — Study {index + 1:02d}',
               'authors': ['Synthetic Author'], 'journal': journal, 'paper_type': kind,
               'year': 2020 + index % 7, 'imported_at': f'2026-09-{index % 28 + 1:02d}T10:00:00+00:00',
               'deleted': False, 'cover': 'cover.png', 'favorite': index % 8 == 0,
               'collection': '界面与损伤' if index % 3 == 0 else '机器人感知',
               'tags': [['微观结构', '疲劳损伤'], ['触觉感知', '机器人'], ['界面力学', '复合材料']][index % 3],
               'pages': [{'number': number, 'width': 600, 'height': 800} for number in range(1, 9)],
               'figures': [], 'blocks': [], 'translation': {'status': 'idle'},
               'summary': '合成文献，仅用于独立界面验收。', 'notes': '合成测试笔记。'}
        if index % 4 == 0:
            doc['last_read'] = f'2026-10-{index % 10 + 1:02d}T10:00:00+00:00'
        if index == 1:
            doc['last_read'] = 'invalid-date'
        if pid == LATEST_ID:
            doc.update(last_read='2026-10-16T10:00:00+00:00', read_page=4, reader_state={'page': 4})
        server.put_doc(doc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='work/library-ui-verification')
    parser.add_argument('--serve-only', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to((ROOT / 'work').resolve()):
        parser.error('测试产物必须位于项目 work/ 内。')
    output.mkdir(parents=True, exist_ok=True)
    data = output / f'data-{uuid.uuid4().hex}'
    seed_library(data)
    forbidden = []
    stop_requested = threading.Event()

    def no_model(*_args, **_kwargs):
        forbidden.append('model-or-cli')
        raise AssertionError('文献库验收禁止调用真实模型或 CLI')

    class Handler(server.Handler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == '/__test/library-state':
                self.send_json({'forbiddenCalls': forbidden, 'latestId': LATEST_ID})
                return
            super().do_GET()

        def do_POST(self):
            if args.serve_only and self.path == '/__test/stop':
                self.read_json()
                self.send_json({'ok': True})
                stop_requested.set()
                return
            super().do_POST()

    listener = server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.PORT = listener.server_port
    worker = threading.Thread(target=listener.serve_forever, daemon=True)
    report = {'status': 'running', 'url': f'http://127.0.0.1:{server.PORT}',
              'app_root': str(ROOT), 'data_dir': str(data), 'realLibraryWrites': 0}
    with ExitStack() as patches:
        for owner, name in [(server, 'codex_status'), (codex_models, 'get'),
                            (codex_models, 'resolve_config'), (paper_ai, 'CodexSession'),
                            (ai_api.ApiSessionPool, 'request_json'),
                            (translation_api.ApiTranslationSession, '_send_json')]:
            patches.enter_context(patch.object(owner, name, side_effect=no_model))
        worker.start()
        print(json.dumps(report, ensure_ascii=False), flush=True)
        try:
            if args.serve_only:
                # Windows 无期限 Event.wait 可能延后 Ctrl+C；短等待保留正常信号处理。
                while not stop_requested.wait(0.25):
                    pass
                report['status'] = 'stopped'
            else:
                runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
                environment = {**os.environ, 'EZREAD_TEST_URL': report['url'], 'EZREAD_TEST_ROOT': str(ROOT),
                               'EZREAD_TEST_OUTPUT': str(output)}
                environment.setdefault('EZREAD_PLAYWRIGHT', str(runtime / 'node_modules/playwright'))
                node = environment.get('EZREAD_NODE') or shutil.which('node') or str(runtime / 'bin/node.exe')
                result = subprocess.run([node, str(ROOT / 'tests/browser_library_ui.cjs')],
                                        env=environment, cwd=ROOT, timeout=180)
                if result.returncode:
                    raise RuntimeError(f'浏览器验收退出码：{result.returncode}')
                assert not forbidden, forbidden
                report['status'] = 'passed'
        except KeyboardInterrupt:
            report['status'] = 'stopped'
        except BaseException as error:
            report.update(status='failed', error=str(error))
            raise
        finally:
            listener.shutdown()
            worker.join(3)
            listener.server_close()
            report.update(forbiddenCalls=forbidden, realModelCalls=0)
            (output / 'server-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
