"""旋转归一化与恢复的快速 roundtrip；完整八页管线验收由独立脚本执行。"""
from pathlib import Path
import subprocess
import tempfile
import unittest

import pdf_layout_engine as engine

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(engine._paths(ROOT)[1].is_file(), '独立 BabelDOC 运行时未安装。')
class PdfRotationTests(unittest.TestCase):
    def run_probe(self, body):
        script = '''import importlib.util,sys,shutil
from pathlib import Path
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('rotation_verifier',sys.argv[1])
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
attempts=h.block_network()
engine=h.engine;fitz=h.pymupdf;folder=Path(sys.argv[2])
''' + body + '\nassert not attempts, attempts\n'
        (ROOT / 'work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='pdf-rotation-unit-', dir=ROOT / 'work') as directory:
            result = subprocess.run([str(engine._paths(ROOT)[1]), '-X', 'utf8', '-c', script,
                                     str(ROOT / 'scripts/verify-pdf-rotation.py'), directory],
                                    cwd=ROOT, env=engine.runtime_environment(ROOT), capture_output=True,
                                    text=True, encoding='utf-8', timeout=45,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_upright_page_is_unchanged_and_avoids_normalization_copy(self):
        self.run_probe('''source=folder/'normal.pdf'
h.make_source(source,[dict(metadata=0,content=0,crop=False)])
before=source.read_bytes()
path,report=engine._normalize_source_rotation(source,folder/'run')
assert path==source and report=={'normalized':False,'pages':[]},report
assert source.read_bytes()==before
''')

    def test_rotations_and_shifted_cropbox_roundtrip_without_model_or_full_pipeline(self):
        self.run_probe('''source=folder/'rotations.pdf'
cases=h.make_source(source)
before=source.read_bytes()
path,report=engine._normalize_source_rotation(source,folder/'run')
assert report['normalized'] and path!=source
plans={p['page']:p for p in report['pages']}
with fitz.open(path) as canonical:
    for index,(page,case) in enumerate(zip(canonical,cases),1):
        assert page.rotation==0 and engine._page_text_rotation(page)==0,(index,case)
        plan=plans.get(index,{'processing_rotation':0,'restore_rotation':0})
        assert plan['processing_rotation']==case['content'],(index,plan)
        assert plan['restore_rotation']==(case['metadata']-case['content'])%360,(index,plan)
mono=folder/'mono.pdf';dual=folder/'dual.pdf';shutil.copyfile(path,mono)
engine._restore_source_rotation(source,mono,dual,report,SimpleNamespace(dual_translate_first=False,input_file=source))
with fitz.open(source) as original,fitz.open(mono) as restored,fitz.open(dual) as both:
    assert len(original)==len(restored)==len(both)==8
    for index,(a,b,d) in enumerate(zip(original,restored,both),1):
        assert a.rotation==b.rotation,(index,a.rotation,b.rotation)
        assert tuple(a.rect)==tuple(b.rect),(index,a.rect,b.rect)
        assert h.equivalent_pixels(h.pixel_comparison(h.raster(a),h.raster(b))),index
        assert h.equivalent_pixels(h.pixel_comparison(h.raster(a),h.raster(d,a.rect))),index
        right=fitz.Rect(a.rect.width,0,2*a.rect.width,a.rect.height)
        assert h.equivalent_pixels(h.pixel_comparison(h.raster(b),h.raster(d,right))),index
assert source.read_bytes()==before
''')

    def test_blank_dual_with_correct_dimensions_is_rejected_without_replacing_prior_dual(self):
        self.run_probe('''from unittest.mock import patch
from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
source=folder/'source.pdf'
h.make_source(source,[dict(metadata=90,content=0,crop=False)])
path,report=engine._normalize_source_rotation(source,folder/'run')
mono=folder/'mono.pdf';dual=folder/'dual.pdf';shutil.copyfile(path,mono)
prior=b'existing private dual evidence';dual.write_bytes(prior)
def blank_dual(self,original,translated,path,config):
    result=fitz.open()
    for a,b in zip(original,translated):
        result.new_page(width=a.rect.width+b.rect.width,height=max(a.rect.height,b.rect.height))
    return result
with patch.object(PDFCreater,'create_side_by_side_dual_pdf',blank_dual):
    try: engine._restore_source_rotation(source,mono,dual,report,SimpleNamespace(dual_translate_first=False))
    except ValueError: pass
    else: raise AssertionError('dimensionally correct blank dual was accepted')
assert dual.read_bytes()==prior
assert not report.get('output_restored')
''')

    def test_native_pdf_annotation_remains_in_source_while_content_roundtrips(self):
        self.run_probe('''source=folder/'annotated.pdf'
h.make_source(source,[dict(metadata=90,content=90,crop=True)])
with fitz.open(source) as document:
    annotation=document[0].add_rect_annot(fitz.Rect(45,45,170,100))
    annotation.set_info(content='Synthetic source annotation')
    annotation.update();document.saveIncr()
before=source.read_bytes()
path,report=engine._normalize_source_rotation(source,folder/'run')
mono=folder/'mono.pdf';dual=folder/'dual.pdf';shutil.copyfile(path,mono)
engine._restore_source_rotation(source,mono,dual,report,SimpleNamespace(dual_translate_first=False))
with fitz.open(source) as original,fitz.open(mono) as restored,fitz.open(dual) as both:
    annotations=list(original[0].annots())
    assert len(annotations)==1 and annotations[0].info['content']=='Synthetic source annotation'
    def content(page,clip=None):
        return page.get_pixmap(colorspace=fitz.csGRAY,alpha=False,annots=False,clip=clip)
    assert h.equivalent_pixels(h.pixel_comparison(content(original[0]),content(restored[0])))
    assert h.equivalent_pixels(h.pixel_comparison(content(original[0]),content(both[0],original[0].rect)))
assert source.read_bytes()==before
''')

    def test_metadata_xmp_and_upstream_retranslation_guard_survive_normalize_and_restore(self):
        self.run_probe('''from babeldoc.format.pdf.high_level import check_metadata,InputFileGeneratedByBabelDOCError
source=folder/'metadata.pdf'
h.make_source(source,[dict(metadata=90,content=90,crop=True)])
producer='BabelDOCv0.6.4_fixture_Translation_generated_by_AI,please_carefully_discern'
title='Synthetic rotation metadata fixture'
xmp=('<x:xmpmeta xmlns:x="adobe:ns:meta/">'
     '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
     '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
     '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">Synthetic rotation metadata fixture</rdf:li></rdf:Alt></dc:title>'
     '</rdf:Description></rdf:RDF></x:xmpmeta>')
with fitz.open(source) as document:
    document.set_metadata({'producer':producer,'title':title})
    document.set_xml_metadata(xmp);document.saveIncr()
before=source.read_bytes()
def check(document):
    assert document.metadata['producer']==producer,document.metadata
    assert document.metadata['title']==title,document.metadata
    assert document.get_xml_metadata()==xmp,document.get_xml_metadata()
    try: check_metadata(document)
    except InputFileGeneratedByBabelDOCError: pass
    else: raise AssertionError('upstream accepted an already-translated producer marker')
path,report=engine._normalize_source_rotation(source,folder/'run')
with fitz.open(path) as canonical: check(canonical)
mono=folder/'mono.pdf';dual=folder/'dual.pdf';shutil.copyfile(path,mono)
engine._restore_source_rotation(source,mono,dual,report,SimpleNamespace(dual_translate_first=False))
for path in (source,mono,dual):
    with fitz.open(path) as document: check(document)
assert source.read_bytes()==before
''')


if __name__ == '__main__':
    unittest.main()
