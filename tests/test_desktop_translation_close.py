"""运行原生后台的关闭预检代码；只访问合成本机 HTTP，不启动桌面或模型。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
COMPILER = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'


@unittest.skipUnless(os.name == 'nt' and COMPILER.is_file(), '需要 Windows .NET Framework 编译器')
class DesktopTranslationCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        folder = Path(cls.temporary.name)
        source = (ROOT / 'desktop/EzRead.Desktop.cs').read_text(encoding='utf-8')
        backend = source.split('internal sealed class OwnedBackend', 1)[1].split('internal sealed class ReaderWindow', 1)[0]
        harness = '''
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
internal sealed class ShellOptions { public Uri Origin; public string Root = "", Data = ""; }
internal static class Probe {
    public static int Main(string[] args) {
        using (var backend = new OwnedBackend(new ShellOptions { Origin = new Uri(args[0]) })) {
            if (args.Length == 1) typeof(OwnedBackend).GetField("attached", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic).SetValue(backend, true);
            try { Console.Write(backend.HasTranslationWork() ? "active" : "idle"); return 0; }
            catch { Console.Write("unavailable"); return 2; }
        }
    }
}
internal sealed class OwnedBackend''' + backend
        code = folder / 'Probe.cs'
        cls.executable = folder / 'Probe.exe'
        code.write_text(harness, encoding='utf-8')
        result = subprocess.run([str(COMPILER), '/nologo', '/reference:System.Web.Extensions.dll',
                                 '/out:' + str(cls.executable), str(code)], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def probe(self, payload, *, attached=True, status=200):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                args = [str(self.executable), f'http://127.0.0.1:{server.server_port}/']
                if not attached:
                    args.append('unattached')
                result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            finally:
                server.shutdown()
                thread.join(timeout=5)
        self.assertEqual(paths, ['/api/papers'] if attached else [])
        return result.returncode, result.stdout

    def test_running_and_queued_pdf_or_legacy_translation_require_confirmation(self):
        for name in ('translation', 'pdf_layout_translation'):
            for status in ('running', 'queued'):
                with self.subTest(name=name, status=status):
                    self.assertEqual(self.probe({'papers': [{name: {'status': status}}]}), (0, 'active'))

    def test_idle_paused_completed_and_unrelated_tasks_do_not_prompt(self):
        for paper in ({}, {'translation': {'status': 'paused'}}, {'pdf_layout_translation': {'status': 'completed'}},
                      {'summarize_status': 'running', 'reading_structure': {'status': 'queued'}}):
            with self.subTest(paper=paper):
                self.assertEqual(self.probe({'papers': [paper]}), (0, 'idle'))
        self.assertEqual(self.probe({'papers': []}), (0, 'idle'))

    def test_malformed_or_failed_status_is_not_treated_as_idle(self):
        for payload in ({}, {'papers': {}}, {'papers': [None]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.probe(payload), (2, 'unavailable'))
        self.assertEqual(self.probe({'error': 'not available'}, status=503), (2, 'unavailable'))

    def test_failed_startup_does_not_query_or_take_over_an_existing_backend(self):
        self.assertEqual(self.probe({}, attached=False), (0, 'idle'))

    def test_large_library_status_is_not_limited_by_default_json_parser_size(self):
        self.assertEqual(self.probe({'papers': [{'summary': 'x' * (2 * 1024 * 1024)},
                                              {'pdf_layout_translation': {'status': 'queued'}}]}), (0, 'active'))


if __name__ == '__main__':
    unittest.main()
