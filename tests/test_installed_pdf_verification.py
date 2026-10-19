"""安装版 PDF 验收入口的隔离边界和合成输入回归。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pypdfium2 as pdfium


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'verify-installed-pdf.py'
spec = importlib.util.spec_from_file_location('installed_pdf_verification', SCRIPT)
verification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verification)


class InstalledPdfVerificationTests(unittest.TestCase):
    def test_fixture_has_two_real_pages_and_stable_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.pdf'
            verification.fixture_pdf(path)
            before = verification.digest(path)
            with pdfium.PdfDocument(path) as document:
                self.assertEqual(len(document), 2)
                for page in document:
                    self.assertEqual((page.get_width(), page.get_height()), (600, 800))
                    self.assertIn('synthetic paragraph', page.get_textpage().get_text_range())
            self.assertEqual(verification.digest(path), before)

    def test_rejects_output_outside_project_without_creating_it(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'must-not-exist'
            result = subprocess.run([sys.executable, str(SCRIPT), '--app-root', folder,
                                     '--output', str(output)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())

    def test_rejects_existing_output_without_touching_contents(self):
        root = SCRIPT.parent.parent
        result = subprocess.run([sys.executable, str(SCRIPT), '--app-root', str(root),
                                 '--output', str(root / 'work')], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_worker_refuses_existing_data(self):
        with tempfile.TemporaryDirectory(dir=SCRIPT.parent.parent / 'work') as folder:
            data = Path(folder) / 'data'
            data.mkdir()
            sentinel = data / 'keep.txt'
            sentinel.write_text('preserved', encoding='utf-8')
            result = subprocess.run([sys.executable, str(SCRIPT), '--worker',
                                     '--app-root', str(SCRIPT.parent.parent),
                                     '--output', folder], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'preserved')

    def test_network_guard_blocks_before_external_connect(self):
        code = '''import importlib.util,json,socket,sys
spec=importlib.util.spec_from_file_location('verification',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
network={'blocked_external_connections':0,'loopback_connections':0}
m.install_network_guard(network)
try:
    with socket.socket() as s: s.connect(('192.0.2.1',443))
except RuntimeError:
    print(json.dumps(network))
else:
    raise AssertionError('external connection was not blocked')
'''
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'blocked_external_connections': 1,
                                                    'loopback_connections': 0})


if __name__ == '__main__':
    unittest.main()
