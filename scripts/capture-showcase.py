"""用完全合成的独立文献库拍摄当前界面；不读取真实库或调用模型。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TITLES = [
    'Geometry-guided design of layered materials',
    'Adaptive tactile sensing for soft robotic systems',
    'Mapping interface networks in architected solids',
    'A synthetic atlas of fatigue crack pathways',
    'Learning compact representations of microstructure',
    'Thermal transport across patterned interfaces',
    'Strain localization in cellular metamaterials',
    'Multiscale contact mechanics for compliant grippers',
    'Spatial ordering in lightweight alloy architectures',
    'Interpretable models for materials discovery',
    'A geometric view of damage accumulation',
    'Process maps for reproducible mechanical design',
    'Controlling anisotropy through structural motifs',
    'Open workflows for visual research notebooks',
    'Topology and transport in connected networks',
    'Benchmarking synthetic microscopy descriptors',
]


def generate_pdfs(destination):
    """由已有隔离引擎执行；全部文字、曲线及图形均为本脚本创作。"""
    import pymupdf as fitz

    destination.mkdir(parents=True, exist_ok=True)
    ink, blue, green = (0.16, 0.21, 0.25), (0.30, 0.49, 0.64), (0.39, 0.58, 0.50)

    def text(page, box, content, size=11, chinese=False, color=ink):
        result = page.insert_textbox(fitz.Rect(box), content, fontsize=size,
                                     fontname='china-s' if chinese else 'helv', color=color,
                                     lineheight=1.3)
        if result < 0:
            raise ValueError(f'Synthetic PDF text overflow: {content[:35]}')

    def figure(page):
        page.draw_rect(fitz.Rect(44, 420, 576, 650), color=None, fill=(0.96, 0.97, 0.96))
        for row in range(4):
            for column in range(5):
                x, y = 76+column*43, 457+row*43
                page.draw_circle((x, y), 12+(row+column)%4, color=None, fill=blue if (row+column)%2 else green)
                if column < 4:
                    page.draw_line((x+16, y), (x+27, y), color=(0.72, 0.77, 0.78), width=1.4)
        page.draw_line((330, 610), (548, 610), color=ink, width=1)
        page.draw_line((330, 610), (330, 458), color=ink, width=1)
        for index, color in enumerate((blue, green)):
            points = [(330+x, 604-(108+index*21)*(1-math.exp(-x/(53+index*33)))) for x in range(210)]
            shape = page.new_shape()
            shape.draw_polyline(points)
            shape.finish(color=color, width=2.6, closePath=False)
            shape.commit()
        text(page, (60, 625, 285, 648), '(a) Geometric interface network', 9)
        text(page, (338, 625, 560, 648), '(b) Illustrative response curves', 9)

    for index, title in enumerate(TITLES):
        document = fitz.open()
        for number in range(2):
            page = document.new_page(width=620, height=820)
            text(page, (44, 28, 576, 49), 'EZREAD  /  SYNTHETIC DEMONSTRATION  /  NO EXPERIMENTAL DATA', 8, color=blue)
            text(page, (44, 69, 576, 132), title if number == 0 else 'From geometry to a reproducible workflow', 23)
            text(page, (44, 144, 576, 173), 'Demo Research Group  |  Synthetic Research Notes  |  2026', 9)
            page.draw_line((44, 181), (576, 181), color=(0.82, 0.85, 0.86), width=0.8)
            text(page, (44, 198, 576, 232), 'Abstract' if number == 0 else 'Methods and interpretation', 15)
            text(page, (44, 233, 576, 310),
                 'This fully synthetic article demonstrates a local reading workflow. We construct geometric '
                 'networks and illustrative response curves to explore how structure can guide interpretation. '
                 'All values, figures and conclusions are invented for interface demonstration.', 11)
            text(page, (44, 321, 293, 405),
                 '1. A controlled visual example\nRegular motifs define an interface network. The colored nodes show two '
                 'illustrative structural families, without representing any measured material.', 10)
            text(page, (323, 321, 576, 405),
                 '2. Reading across languages\nThe companion Chinese PDF is manually authored for this demonstration. '
                 'It is not a model output or a translation-quality benchmark.', 10)
            figure(page)
            text(page, (44, 668, 576, 710),
                 'Figure 1. Programmatically generated geometry and hypothetical curves. '
                 'No published figure, source paper or experimental observation is reproduced.', 9)
            text(page, (44, 735, 576, 773),
                 'Demonstration only. Original text and diagrams were created for EzRead screenshots.', 9, color=blue)
            text(page, (44, 789, 576, 811), f'SYNTHETIC DEMO                                                    {number+1} / 2', 8)
        document.save(destination / f'original-{index:02d}.pdf', garbage=4, deflate=True)
        document.close()
    document = fitz.open()
    for number in range(2):
        page = document.new_page(width=620, height=820)
        text(page, (44, 28, 576, 52), '合成演示 / 手写模拟译文 / 不代表真实模型翻译效果', 10, True, blue)
        text(page, (44, 70, 576, 137), '几何引导的层状材料设计' if number == 0 else '从几何结构到可复现工作流', 25, True)
        text(page, (44, 147, 576, 173), '演示研究组  |  合成研究札记  |  2026', 10, True)
        page.draw_line((44, 181), (576, 181), color=(0.82, 0.85, 0.86), width=0.8)
        text(page, (44, 199, 576, 230), '摘要' if number == 0 else '方法与理解', 17, True)
        text(page, (44, 243, 576, 310),
             '本文为完全合成的演示文献，用于展示本地阅读流程。我们构建几何网络和假设响应曲线，'
             '说明结构如何帮助组织研究问题。所有数值、图形和结论均为界面展示而虚构。', 12, True)
        text(page, (44, 326, 294, 407),
             '1. 可控的视觉示例\n规则单元构成界面网络。不同颜色仅区分两组假想结构，不对应任何实测材料。', 11, True)
        text(page, (323, 326, 576, 407),
             '2. 跨语言对照阅读\n此中文 PDF 为演示而手写，保留原文的页面结构。它不是模型输出，也不用于评价翻译质量。', 11, True)
        figure(page)
        text(page, (44, 669, 576, 720),
             '图 1. 程序生成的几何结构与假设曲线。未使用已发表图片、真实论文或实验数据。', 11, True)
        text(page, (44, 744, 576, 778), '仅供合成演示；文字与图形均为本项目截图专门创作。', 10, True, blue)
        text(page, (44, 789, 576, 811), f'SYNTHETIC DEMO                                                    {number+1} / 2', 8)
    document.save(destination / 'mono.pdf', garbage=4, deflate=True)
    document.close()
    with fitz.open(destination / 'original-00.pdf') as english, fitz.open(destination / 'mono.pdf') as chinese:
        dual = fitz.open()
        for number in range(2):
            page = dual.new_page(width=1240, height=820)
            page.show_pdf_page(fitz.Rect(0, 0, 620, 820), english, number)
            page.show_pdf_page(fitz.Rect(620, 0, 1240, 820), chinese, number)
        dual.save(destination / 'dual.pdf', garbage=4, deflate=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate-pdfs', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--node', default=os.environ.get('EZREAD_NODE', r'D:\APP\Node\node.exe'))
    args = parser.parse_args()
    if args.generate_pdfs:
        generate_pdfs(args.generate_pdfs)
        return
    output = ROOT / 'output/playwright' / f'showcase-{uuid.uuid4().hex}'
    data, pdfs = output / 'data', output / 'synthetic-pdfs'
    output.mkdir(parents=True)
    os.environ['EZREAD_DATA_DIR'] = str(data)
    sys.path.insert(0, str(ROOT))
    import server
    import paper_ai
    from ezread import ai_api, api_settings, translation_api

    spec = importlib.util.spec_from_file_location('library_fixture', ROOT / 'scripts/verify-library-ui.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    server.DATA, server.LIBRARY = data, data / 'library'
    server.init_db()
    subprocess.run([str(ROOT / '.deps/babeldoc/runtime/Scripts/python.exe'), '-B', str(Path(__file__)),
                    '--generate-pdfs', str(pdfs)], cwd=ROOT, check=True,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    sources = [('ACS Nano', 'journal'), ('ACTA MECHANICA SINICA', 'journal'),
               ('Synthetic Research Notes', 'journal'), ('Synthetic Design Conference', 'conference'),
               ('arXiv', 'preprint')]
    for index, title in enumerate(TITLES):
        pid = f'{index:016x}'
        folder = server.LIBRARY / pid
        folder.mkdir(parents=True)
        fixture.synthetic_cover(index).save(folder / 'cover.png')
        shutil.copyfile(pdfs / f'original-{index:02d}.pdf', folder / 'original.pdf')
        journal, kind = sources[index % len(sources)]
        pages = [{'number': n, 'width': 620, 'height': 820} for n in (1, 2)]
        doc = {'id': pid, 'hash': f'synthetic-showcase-{index}', 'title': title,
               'authors': ['Demo Research Group'], 'journal': journal, 'paper_type': kind,
               'year': 2026, 'imported_at': f'2026-10-{18-index:02d}T10:00:00+00:00',
               'deleted': False, 'cover': 'cover.png', 'favorite': index % 5 == 0,
               'collection': '合成演示 · 材料与界面' if index % 2 == 0 else '合成演示 · 计算与感知',
               'tags': [['几何设计', '界面力学'], ['触觉感知', '计算方法'], ['结构表征', '研究工作流']][index % 3],
               'pages': pages, 'figures': [], 'blocks': [], 'translation': {'status': 'idle'},
               'summary': '完全合成的演示文献；正文、图形和封面均为程序生成。中文为手写模拟译文，不代表真实模型效果。',
               'notes': '合成演示：无实验数据、无真实作者、无模型调用。'}
        if index == 0:
            artifact_id = '1234567890abcdef'
            artifact_dir = folder / 'pdf-layout' / artifact_id
            artifact_dir.mkdir(parents=True)
            for name in ('mono.pdf', 'dual.pdf'):
                shutil.copyfile(pdfs / name, artifact_dir / name)
            artifact = {'id': artifact_id, 'created_at': '2026-10-18',
                        'config': {'model': 'handwritten-synthetic-demo'}, 'page_count': 2,
                        'quality': 'passed', 'warnings': [], 'mono': 'mono.pdf', 'dual': 'dual.pdf', 'pages': pages}
            doc.update(last_read='2026-10-18T10:00:00+00:00', read_page=1, reader_state={'page': 1},
                       pdf_layout_current=artifact, pdf_layout_versions=[artifact],
                       pdf_layout_translation={'status': 'completed'})
        server.put_doc(doc)
    forbidden = []

    def no_model(*_args, **_kwargs):
        forbidden.append('model-call')
        raise AssertionError('Showcase forbids model calls')

    class Handler(server.Handler):
        def log_message(self, *_args):
            pass

    listener = server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.PORT = listener.server_port
    worker = threading.Thread(target=listener.serve_forever, daemon=True)
    report = {'status': 'running', 'syntheticOnly': True, 'realLibraryWrites': 0,
              'data_dir': str(data), 'app_root': str(ROOT), 'output': str(output)}
    try:
        with ExitStack() as patches:
            patches.enter_context(patch.object(api_settings, '_read',
                                  return_value={'selected_provider': 'deepseek', 'profiles': {}}))
            for owner, name in [(paper_ai, 'CodexSession'), (ai_api.ApiSessionPool, 'request_json'),
                                (translation_api.ApiTranslationSession, '_send_json')]:
                patches.enter_context(patch.object(owner, name, side_effect=no_model))
            worker.start()
            environment = {**os.environ, 'EZREAD_TEST_URL': f'http://127.0.0.1:{server.PORT}',
                           'EZREAD_TEST_ROOT': str(ROOT), 'EZREAD_TEST_DATA': str(data),
                           'EZREAD_TEST_OUTPUT': str(output)}
            runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
            environment.setdefault('EZREAD_PLAYWRIGHT', str(runtime / 'node_modules/playwright'))
            subprocess.run([args.node, str(ROOT / 'tests/capture_showcase.cjs')], cwd=ROOT,
                           env=environment, timeout=180, check=True)
            assert not forbidden, forbidden
            report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error=str(error))
        raise
    finally:
        listener.shutdown()
        worker.join(3)
        listener.server_close()
        report.update(forbiddenCalls=forbidden, realModelCalls=0, privateConfigReads=0)
        (output / 'server-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
