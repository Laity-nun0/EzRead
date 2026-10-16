"""合成 PDF 的离线旋转验收；真实 BabelDOC 管线只接收模拟译文，不访问 API。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import traceback
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pdf_layout_engine as engine

try:
    import pymupdf
except ImportError:
    pymupdf = None

PREFIX = '离线模拟 '
BODY = 'The synthetic material retains grains and fatigue strength during controlled processing.'
REGIONS = {'formula': (55, 225, 330, 294), 'table': (50, 320, 550, 440),
           'image': (65, 500, 235, 620), 'vectors': (300, 500, 550, 640)}
CASES = [*(dict(metadata=angle, content=0, crop=False) for angle in (0, 90, 180, 270)),
         *(dict(metadata=angle, content=angle, crop=False) for angle in (90, 180, 270)),
         dict(metadata=90, content=90, crop=True)]


def request_units(request):
    # 验收运行时不依赖主应用的数据库/PDFium；仅读取固定上游提示词的数据尾部。
    prompt = request['prompt']
    if request.get('rate_limit_params', {}).get('request_json_mode') is True:
        return json.loads(prompt.rsplit('## Here is the input:\n\n', 1)[1]), True
    if request.get('mode') == 'text':
        return [{'id': 0, 'input': prompt}], False
    return [{'id': 0, 'input': prompt.split('Now translate the following text:\n\n', 1)[1]}], False


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def block_network():
    """仅放行标准库 socketpair 的内部回环连接，包含 Windows 新建事件循环。"""
    attempts = []
    original_connect = socket.socket.connect
    socket_file = Path(socket.__file__).resolve()
    def connect(sock, address):
        caller = sys._getframe(1)
        if caller.f_code.co_name == 'socketpair' and Path(caller.f_code.co_filename).resolve() == socket_file:
            return original_connect(sock, address)
        return denied('connect')
    def denied(operation):
        attempts.append(operation)
        raise RuntimeError('离线旋转验证禁止网络连接。')
    socket.socket.connect = connect
    socket.socket.connect_ex = lambda *_args, **_kwargs: denied('connect_ex')
    socket.create_connection = lambda *_args, **_kwargs: denied('create_connection')
    socket.getaddrinfo = lambda *_args, **_kwargs: denied('getaddrinfo')
    return attempts


def make_source(path, cases=CASES):
    """已知区域的非方形合成页；正角内容流旋转与同角 /Rotate 互相抵消。"""
    with pymupdf.open() as base, pymupdf.open() as output:
        page = base.new_page(width=600, height=800)
        page.insert_text((50, 55), 'Synthetic rotation verification', fontsize=16)
        for row in range(6):
            page.insert_text((50, 85 + row * 20), BODY, fontsize=10)
        page.insert_text((70, 260), 'F = ma + x', fontname='tiit', fontsize=20)
        page.insert_text((180, 250), '2', fontsize=10)
        page.draw_line((70, 273), (215, 273), width=1)
        page.insert_text((100, 290), '2 pi', fontname='tiit', fontsize=14)
        for x in (50, 175, 300, 425, 550):
            page.draw_line((x, 320), (x, 440), width=1.5)
        for y in (320, 360, 400, 440):
            page.draw_line((50, y), (550, y), width=1.5)
        for row in range(3):
            for col in range(4):
                page.insert_text((65 + 125 * col, 345 + 40 * row), str(10 + row * 4 + col), fontsize=12)
        bitmap = bytes(value for y in range(36) for x in range(48)
                       for value in ((30, 85, 185) if (x // 6 + y // 6) % 2 else (215, 55, 40)))
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, 48, 36, bitmap, False)
        page.insert_image(pymupdf.Rect(REGIONS['image']), pixmap=pixmap)
        page.draw_rect((310, 510, 385, 615), color=(.1, .2, .8), fill=(.2, .6, .9), width=3)
        page.draw_circle((470, 555), 35, color=(.8, .1, .1), fill=(.9, .4, .2), width=3)
        page.draw_line((385, 610), (525, 625), color=(.1, .5, .1), width=5)
        for case in cases:
            angle = case['content']
            width, height = (800, 600) if angle in (90, 270) else (600, 800)
            offset = (35, 25) if case['crop'] else (0, 0)
            target = output.new_page(width=width + 2 * offset[0], height=height + 2 * offset[1])
            if case['crop']:
                target.set_cropbox(pymupdf.Rect(offset[0], offset[1], offset[0] + width, offset[1] + height))
            target.show_pdf_page(target.rect, base, 0, rotate=angle)
            target.set_rotation(case['metadata'])
        output.save(path, garbage=3, deflate=True)
    return [dict(case) for case in cases]


def raster(page, clip=None):
    return page.get_pixmap(matrix=pymupdf.Matrix(1, 1), colorspace=pymupdf.csRGB, alpha=False, clip=clip)


def pixel_comparison(before, after):
    same_size = (before.width, before.height, before.n) == (after.width, after.height, after.n)
    result = {'pixel_dimensions_equal': same_size, 'source_ink': sum(v < 220 for v in before.samples),
              'target_ink': sum(v < 220 for v in after.samples)}
    if not same_size:
        return result
    differences = [abs(a - b) for a, b in zip(before.samples, after.samples)]
    retained = sum(a < 220 and b < 235 for a, b in zip(before.samples, after.samples))
    result.update(mean_channel_difference=sum(differences) / max(1, len(differences)),
                  changed_channel_ratio=sum(delta > 8 for delta in differences) / max(1, len(differences)),
                  retained_ink_ratio=retained / max(1, result['source_ink']))
    return result


def equivalent_pixels(result):
    return (result['pixel_dimensions_equal'] and result['mean_channel_difference'] <= .25
            and result['changed_channel_ratio'] <= .003)


def display_region(page, case, coordinates):
    rotation = pymupdf.Matrix(-case['content'])
    rotated = pymupdf.Rect(0, 0, 600, 800) * rotation
    transform = rotation * pymupdf.Matrix(1, 0, 0, 1, -rotated.x0, -rotated.y0)
    return pymupdf.Rect(coordinates) * transform * page.rotation_matrix


def inspect_pages(source_path, mono_path, dual_path, cases, rotation_report, output):
    plans = {item['page']: item for item in rotation_report.get('pages', [])}
    pages = []
    with pymupdf.open(source_path) as source, pymupdf.open(mono_path) as mono, pymupdf.open(dual_path) as dual:
        if not len(source) == len(mono) == len(dual) == len(cases):
            raise ValueError('源文、单语和双语页数不一致。')
        for index, (before, after, both, case) in enumerate(zip(source, mono, dual, cases), 1):
            plan = plans.get(index, {})
            row = {'page': index, 'case': case, 'original_rotation': before.rotation,
                   'processing_rotation': plan.get('processing_rotation', 0),
                   'restore_rotation': plan.get('restore_rotation', before.rotation), 'failures': []}
            size = before.rect
            geometry = (abs(after.rect.width - size.width) < .01 and abs(after.rect.height - size.height) < .01
                        and abs(both.rect.width - 2 * size.width) < .01 and abs(both.rect.height - size.height) < .01)
            row['geometry_equal'] = geometry
            row['orientation_equal'] = after.rotation == before.rotation and engine._page_text_rotation(after) == case['content']
            # PDF 字体可把可见空格映射成 U+0003；只用实际字母数字定位合成正文标记。
            body_text = ''.join(char for char in after.get_text() if char.isalnum())
            row['translated_body_visible'] = '离线模拟Thesyntheticmaterial' in body_text
            for name in ('geometry_equal', 'orientation_equal', 'translated_body_visible'):
                if not row[name]:
                    row['failures'].append(name)
            raster(before).save(output / f'page-{index:02}-source.png')
            raster(after).save(output / f'page-{index:02}-mono.png')
            raster(both).save(output / f'page-{index:02}-dual.png')
            left = pixel_comparison(raster(before), raster(both, size))
            right_clip = pymupdf.Rect(size.width, 0, size.width * 2, size.height)
            right = pixel_comparison(raster(after), raster(both, right_clip))
            row.update(dual_source_half=left, dual_target_half=right, regions={})
            for name, value in (('dual_source_half', left), ('dual_target_half', right)):
                if not equivalent_pixels(value):
                    row['failures'].append(name)
            for name, coords in REGIONS.items():
                clip = display_region(before, case, coords)
                a, b = raster(before, clip), raster(after, clip)
                a.save(output / f'page-{index:02}-{name}-source.png')
                b.save(output / f'page-{index:02}-{name}-mono.png')
                value = pixel_comparison(a, b)
                value['display_clip'] = list(clip)
                minimum = .4 if name == 'formula' else .65
                value['visible'] = (value['pixel_dimensions_equal'] and value['source_ink'] >= 40
                                    and value['target_ink'] >= .7 * value['source_ink']
                                    and value.get('retained_ink_ratio', 0) >= minimum
                                    and value.get('mean_channel_difference', 255) <= 20)
                row['regions'][name] = value
                if not value['visible']:
                    row['failures'].append(name + '_visible')
            pages.append(row)
    return pages


def worker_stub(path):
    path.write_text(f'''import asyncio,importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("rotation_verifier", {str(Path(__file__).resolve())!r})
harness=importlib.util.module_from_spec(spec);spec.loader.exec_module(harness)
engine=harness.engine
attempts=harness.block_network()
if __name__ == "__main__":
    engine.processes.attach_job()
    request=engine._read(sys.argv[2])
    sys.stdout=sys.stderr
    try:
        asyncio.run(engine._worker(request))
    finally:
        engine._write(Path(request["run_dir"])/"network-attempts.json", attempts)
''', encoding='utf-8')


def verify(output):
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('验证目录非空，拒绝覆盖已有证据；请指定新的 --output 目录。')
    source = output / 'synthetic-rotations.pdf'
    report = {'passed': False, 'real_model_calls': 0, 'raster_colorspace': 'RGB', 'pages': [], 'failures': [],
              'limitations': ['仅验证合成版式、当前独立 BabelDOC 运行时及模拟响应，不代表真实模型语义质量或任意 PDF。']}
    started, calls = time.monotonic(), []
    parent_network = block_network()
    run = output / 'run'
    try:
        cases = make_source(source)
        report['cases'] = cases
        original_hash = engine._sha(source)
        report['source_sha256'] = original_hash
        stub = output / 'offline-worker.py'
        worker_stub(stub)
        original_spawn = engine.processes.spawn
        def spawn(command, **kwargs):
            if Path(command[1]).resolve() == ROOT / 'pdf_layout_engine.py':
                command = [command[0], str(stub), '--worker', command[-1]]
            return original_spawn(command, **kwargs)
        def translate(request):
            units, batch = request_units(request)
            values = [{'id': unit['id'], 'output': PREFIX + unit['input']} for unit in units]
            response = json.dumps(values, ensure_ascii=False) if batch else values[0]['output']
            calls.append({'request': request, 'response': response})
            save_json(output / 'requests.json', calls)
            return response
        # 仅更换隔离进程入口来封锁网络；生产管线、原始提示词及排版方法不替换。
        with patch.object(engine.processes, 'spawn', side_effect=spawn):
            result = engine.run_native(ROOT, source, run, translate=translate, glossary=[])
        report['result'] = result
    except Exception as exc:
        report['failures'].append(type(exc).__name__ + ': ' + str(exc))
        (output / 'exception.txt').write_text(traceback.format_exc(), encoding='utf-8')
        if (output / 'run/rotation-normalization.json').is_file():
            report['rotation_normalization'] = engine._read(output / 'run/rotation-normalization.json')
    finally:
        # 即使生产管线拒绝发布，也检查已经生成的合成产物并保留丢失区域证据。
        try:
            outputs = {}
            for kind in ('mono', 'dual'):
                path = run / report['result'][kind + '_pdf'] if 'result' in report else None
                candidates = [path] if path is not None and path.is_file() else list((run / 'native/pdf').glob('*.' + kind + '.pdf'))
                if len(candidates) == 1:
                    outputs[kind] = output / (kind + '.pdf')
                    shutil.copyfile(candidates[0], outputs[kind])
            if len(outputs) == 2:
                rotation = engine._read(run / 'rotation-normalization.json')
                report['pages'] = inspect_pages(source, outputs['mono'], outputs['dual'], cases, rotation, output)
                for page in report['pages']:
                    report['failures'].extend(f"page-{page['page']}:{name}" for name in page['failures'])
        except Exception as exc:
            report['failures'].append('artifact_inspection: ' + str(exc))
        report['network_attempts'] = [*parent_network, *(engine._read(run / 'network-attempts.json')
                                                        if (run / 'network-attempts.json').is_file() else [])]
        if report['network_attempts']:
            report['failures'].append('network_attempts')
        if not (run / 'network-attempts.json').is_file():
            report['failures'].append('network_audit_missing')
        report['seconds'] = round(time.monotonic() - started, 3)
        report['requests'] = len(calls)
        report['source_hash_unchanged'] = source.is_file() and engine._sha(source) == report.get('source_sha256')
        if not report['source_hash_unchanged']:
            report['failures'].append('source_hash_changed')
        report['passed'] = bool(report['pages']) and not report['failures']
        save_json(output / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    output = (args.output or ROOT / 'work' / ('pdf-rotation-' + datetime.now().strftime('%Y%m%d-%H%M%S')
                                             + '-' + uuid.uuid4().hex[:8])).resolve()
    if not output.is_relative_to((ROOT / 'work').resolve()):
        parser.error('--output 必须位于项目 work/ 下，禁止使用真实文献库。')
    if pymupdf is None:
        runtime = engine._paths(ROOT)[1]
        if not runtime.is_file():
            parser.error('独立 BabelDOC 运行时未安装，未执行验证。')
        result = subprocess.run([str(runtime), '-X', 'utf8', str(Path(__file__).resolve()), '--output', str(output)],
                                cwd=ROOT, env=engine.runtime_environment(ROOT),
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.returncode
    report = verify(output)
    print(json.dumps({'passed': report['passed'], 'report': str((output / 'report.json').relative_to(ROOT)),
                      'failures': report['failures']}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
