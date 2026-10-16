"""原生术语与格式恢复的真实本地排版回归；模型只用离线回调。"""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import pdf_layout_engine as engine
from ezread.pdf_native_translation import RequestCache, request_units
from ezread import pdf_translations as layout

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 BabelDOC 运行时未安装。')
class StandardNativePipelineTests(unittest.TestCase):
    def make_source(self, source):
        script = '''import pymupdf,sys
with pymupdf.open() as doc:
    page = doc.new_page(width=600,height=800)
    for row in range(6):
        page.insert_text((50,80+40*row), 'The grain size affects strength and fatigue in the synthetic material.', fontsize=10)
    doc.save(sys.argv[1])
'''
        subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script, str(source)],
                       cwd=ROOT, env=engine.runtime_environment(ROOT), check=True, capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_native_fenced_batch_is_cleaned_upstream_without_single_retry(self):
        with tempfile.TemporaryDirectory(prefix='native-v3-fenced-', dir=ROOT / 'work') as directory:
            folder = Path(directory)
            source = folder / 'synthetic.pdf'
            self.make_source(source)
            identity = {'source_hash': layout.file_hash(source), 'config': {'model': 'offline'},
                        'engine_version': engine.ENGINE_VERSION, 'protocol': layout.NATIVE_PROTOCOL}
            cache = RequestCache(folder / 'cache', identity, native_flow=True)
            calls, replies = [], []

            def offline(request):
                units, batch = request_units(request)
                calls.append(batch)
                self.assertTrue(batch, '合法围栏响应不得发起单段补试')
                response = '```json\n' + json.dumps([{'id': u['id'], 'output': '离线模拟 ' + u['input']}
                                                       for u in units], ensure_ascii=False) + '\n```'
                replies.append(response)
                return response

            result = engine.run_native(ROOT, source, folder / 'run',
                                       translate=lambda request: cache.resolve(request, offline), glossary=[])
            self.assertTrue(calls)
            self.assertEqual(result['translation_audit']['fallback_attempts'], 0)
            saved = [json.loads(p.read_text(encoding='utf-8'))['response']
                     for p in (folder / 'cache/native-replies').glob('*.json')]
            self.assertCountEqual(saved, replies, '原始响应不能被上游清理覆盖')

    def test_native_glossary_and_saved_bad_batch_recover_then_replay_without_calls(self):
        with tempfile.TemporaryDirectory(prefix='native-v3-', dir=ROOT / 'work') as directory:
            folder = Path(directory)
            source = folder / 'synthetic.pdf'
            self.make_source(source)
            identity = {'source_hash': layout.file_hash(source), 'config': {'model': 'offline'},
                        'engine_version': engine.ENGINE_VERSION, 'protocol': layout.NATIVE_PROTOCOL}
            cache = RequestCache(folder / 'cache', identity, native_flow=True, retry_rejected=True)
            calls, events = [], []
            seed = [{'source': 'grain size', 'target': '晶粒尺寸'}, {'source': 'unrelated marker', 'target': '无关术语'}]

            def offline(request):
                calls.append(copy.deepcopy(request))
                units, batch = request_units(request)
                if batch:
                    # 可信传输中的完整模型内容格式错误，不是 HTTP 截断。
                    return 'saved malformed batch response'
                return '晶粒尺寸离线模拟 ' + units[0]['input']

            def translate(request):
                events.append(copy.deepcopy(request))
                return cache.resolve(request, offline)

            result = engine.run_native(ROOT, source, folder / 'first', translate=translate, glossary=seed)
            self.assertTrue((folder / 'first' / result['mono_pdf']).is_file())
            self.assertTrue(any(not request_units(event)[1] for event in events), '必须由上游实际安排单段恢复')
            self.assertEqual(sum(request_units(call)[1] for call in calls), 1, '不得为坏批次反复付费')
            self.assertGreater(result['translation_audit']['fallback_attempts'], 0)
            self.assertTrue(all('| grain size | 晶粒尺寸 |' in event['prompt'] for event in events))
            self.assertTrue(all('| unrelated marker |' not in event['prompt'] for event in events))
            self.assertEqual(engine._read(folder / 'first/native-request.json')['glossary'], seed)
            self.assertFalse(engine._read(folder / 'first/native-translation-validation.json')['failures'])
            original = {p.name: p.read_bytes() for p in (folder / 'cache/native-replies').glob('*.json')}
            previous_calls = len(calls)
            cache = RequestCache(folder / 'replay-cache', identity, previous_folder=folder / 'cache',
                                 native_flow=True, retry_rejected=True)

            def replay(request):
                return cache.resolve(request, lambda _: self.fail('重放不得调用模型'))

            replayed = engine.run_native(ROOT, source, folder / 'second', translate=replay, glossary=seed)
            self.assertEqual(len(calls), previous_calls)
            self.assertEqual(cache.model_calls, 0)
            self.assertGreater(cache.reused, 0)
            self.assertEqual(replayed['page_count'], result['page_count'])
            self.assertEqual(original, {p.name: p.read_bytes() for p in (folder / 'replay-cache/native-replies').glob('*.json')})


if __name__ == '__main__':
    unittest.main()
