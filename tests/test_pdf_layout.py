"""Synthetic PDF geometry regressions; no personal PDFs or model calls."""

import unittest
from types import SimpleNamespace

import pdf_tools


def chars(text, x, y, size=10, advance=5, font='Times-Roman'):
    return [{'text': value, 'x0': x + index * advance,
             'x1': x + (index + 1) * advance, 'top': y, 'bottom': y + size,
             'size': size, 'fontname': font, 'upright': True}
            for index, value in enumerate(text)]


def page(characters=(), **objects):
    return SimpleNamespace(width=600, height=800, chars=list(characters),
                           **{name: objects.get(name, [])
                              for name in ('images', 'rects', 'curves', 'lines', 'edges')})


def line(characters):
    return {'text': pdf_tools._line_text(characters),
            'bbox': pdf_tools._union([pdf_tools._box(char) for char in characters]),
            'chars': characters, 'size': characters[0]['size'], 'bold': 0,
            'math': 0, '_column': 'single', '_split': None}


def primitive(x0, top, x1, bottom, **extra):
    return dict(x0=x0, top=top, x1=x1, bottom=bottom, **extra)


class PdfLayoutTests(unittest.TestCase):
    def test_running_header_glyph_does_not_hide_persistent_column_gutter(self):
        characters = []
        text = 'A complete body sentence fills the width of its text column.'
        for y in range(100, 260, 20):
            characters += chars(text, 50, y, advance=235 / len(text))
            characters += chars(text, 315, y, advance=235 / len(text))
        header = chars('H', 282, 45, advance=32)
        synthetic = page(characters + header)
        gutter = pdf_tools._column_gutter(synthetic)
        self.assertIsNotNone(gutter)
        self.assertGreater(gutter[0], 285)
        self.assertLess(gutter[1], 315)
        blocks = pdf_tools._ordered_blocks(pdf_tools._lines(synthetic), 600, 10, [])
        self.assertTrue(any(block['bbox'][2] < 300 and text in block['text'] for block in blocks))
        self.assertTrue(any(block['bbox'][0] > 300 and text in block['text'] for block in blocks))

    def test_temperature_superscripts_stay_in_prose(self):
        for symbol in ('°', '◦'):
            with self.subTest(symbol=symbol):
                characters = []
                for y, temperature in ((100, '300'), (130, '260')):
                    prefix = f'The specimen was held at {temperature}'
                    characters += chars(prefix, 50, y)
                    x = 50 + len(prefix) * 5
                    characters += chars(symbol, x, y - 4, size=5, advance=3, font='Symbol')
                    characters += chars('C before cooling.', x + 3, y)
                extracted = pdf_tools._lines(page(characters))
                self.assertEqual(len(extracted), 2)
                self.assertIn('300°C before cooling.', extracted[0]['text'])
                self.assertIn('260°C before cooling.', extracted[1]['text'])
                blocks = pdf_tools._paragraphs(extracted, 600, 10)
                self.assertTrue(all(not block['is_formula'] for block in blocks))

    def test_temperature_symbol_joins_short_row_instead_of_nearby_prose(self):
        characters = chars('The preceding description extends across the row.', 50, 89)
        characters += chars('450', 140, 100)
        characters += chars('C', 158, 100)
        characters += chars('◦', 155, 94, size=5, advance=3, font='Symbol')
        extracted = pdf_tools._lines(page(characters))
        temperature = next(item for item in extracted if '450' in item['text'])
        self.assertEqual(temperature['text'], '450°C')
        description = next(item for item in extracted if 'preceding' in item['text'])
        self.assertNotIn('°', description['text'])
        self.assertNotIn('◦', description['text'])
        self.assertFalse(pdf_tools._paragraphs([temperature], 600, 10)[0]['is_formula'])

    def test_display_equation_and_fraction_keep_formula_flag(self):
        characters = chars('E = mc²', 220, 200, font='CMMI10')
        characters += chars('σ =', 200, 250, font='CMMI10')
        characters += chars('F', 245, 243, size=8, font='CMMI10')
        characters += chars('A', 245, 262, size=8, font='CMMI10')
        extracted = pdf_tools._lines(page(characters))
        blocks = pdf_tools._paragraphs(extracted, 600, 10)
        self.assertTrue(blocks)
        self.assertTrue(all(block['is_formula'] for block in blocks))
        self.assertTrue(any('E = mc' in block['text'] for block in blocks))
        self.assertTrue(any('F' in block['text'] for block in blocks))
        self.assertTrue(any('A' in block['text'] for block in blocks))

    def test_unmapped_math_font_keeps_equation_number_inside_source_crop(self):
        characters = chars('Crack propagation rate ', 310, 140, size=8, advance=3)
        characters += chars('¼', 380, 140, size=8, advance=4, font='AdvP4C4E74')
        characters += chars('L', 400, 135, size=8, advance=4, font='AdvPSMP10')
        characters += chars('t', 400, 149, size=8, advance=4, font='AdvGulliv-I')
        characters += chars('ð2Þ', 541, 140, size=8, advance=4, font='AdvP4C4E74')
        extracted = pdf_tools._lines(page(characters))
        blocks = pdf_tools._merge_formula_regions(pdf_tools._paragraphs(extracted, 600, 8))
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0]['is_formula'])
        self.assertGreaterEqual(blocks[0]['bbox'][2], 553)
        self.assertGreaterEqual(blocks[0]['bbox'][3], 157)

    def test_wide_equation_fragments_stay_together_without_prose_lead(self):
        pieces = [chars('by', 310, 100, size=8),
                  chars('0', 415, 112, size=8, font='AdvP4C4E74'),
                  chars('i2', 470, 112, size=5),
                  chars('γ = F', 310, 124, size=8, font='Symbol'),
                  chars('ð3Þ', 541, 124, size=8, advance=4, font='AdvP4C4E74'),
                  chars('SSD SSD GND', 340, 133, size=5)]
        lines = [dict(line(piece), _column='right') for piece in pieces]
        for item in lines:
            item['math'] = 1 if any('AdvP4C' in c['fontname'] or c['fontname'] == 'Symbol' for c in item['chars']) else 0
        blocks = pdf_tools._merge_formula_regions(pdf_tools._paragraphs(lines, 600, 8))
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]['text'], 'by')
        self.assertFalse(blocks[0]['is_formula'])
        self.assertTrue(blocks[1]['is_formula'])
        self.assertGreaterEqual(blocks[1]['bbox'][2], 553)
        self.assertGreaterEqual(blocks[1]['bbox'][3], 138)

    def test_table_references_in_prose_are_not_table_captions(self):
        for text in ('Table 1. Hereinafter the samples are named by condition.',
                     'Table 2 shows the results obtained after processing.'):
            with self.subTest(text=text):
                extracted = pdf_tools._lines(page(chars(text, 50, 100)))
                blocks = pdf_tools._paragraphs(extracted, 600, 10)
                self.assertEqual(blocks[0]['kind'], 'paragraph')
                self.assertFalse(blocks[0].get('is_table', False))
                self.assertEqual(pdf_tools._table_blocks(page(), extracted), [])

    def test_ruled_spanning_table_keeps_both_columns(self):
        characters = chars('Table 3', 50, 100)
        for y, left, right in ((130, 'Condition', 'Hardness'),
                               (150, 'Annealed', '95'), (170, 'Aged', '150')):
            characters += chars(left, 55, y)
            characters += chars(right, 350, y)
        edges = [primitive(50, y, 550, y, orientation='h') for y in (120, 145, 190)]
        synthetic = page(characters, edges=edges)
        tables = pdf_tools._table_blocks(synthetic, pdf_tools._lines(synthetic))
        self.assertEqual(len(tables), 2)
        caption, body = tables
        self.assertEqual(caption['text'], 'Table 3')
        self.assertEqual(caption['kind'], 'caption')
        self.assertFalse(caption.get('is_table'))
        self.assertTrue(body['is_table'])
        self.assertEqual(body['kind'], 'table')
        self.assertEqual(body['text'].splitlines(),
                         ['Condition | Hardness', 'Annealed | 95', 'Aged | 150'])
        self.assertLessEqual(body['bbox'][0], 50)
        self.assertGreaterEqual(body['bbox'][2], 550)
        self.assertLess(caption['bbox'][3], body['bbox'][1])

    def test_multi_panel_figure_contains_labels_and_excludes_caption(self):
        panels = [primitive(x, y, x + 200, y + 90)
                  for y in (180, 285) for x in (80, 330)]
        characters = []
        for x, y, label in ((58, 170, 'a'), (308, 170, 'b'),
                            (58, 275, 'c'), (308, 275, 'd')):
            characters += chars(label, x, y, size=8)
        characters += chars('100', 58, 205, size=8)
        characters += chars('0', 530, 360, size=8)
        characters += chars('20', 250, 378, size=8)
        characters += chars('Fig. 1. Four panels show the measured response.', 50, 405)
        synthetic = page(characters, images=panels)
        candidates = pdf_tools._graphic_candidates(synthetic, pdf_tools._lines(synthetic))
        containing = [box for box in candidates if box[0] <= 80 and box[2] >= 530
                      and box[1] <= 180 and box[3] >= 375]
        self.assertEqual(len(containing), 1)
        box = containing[0]
        for char in characters:
            if char['top'] < 400:
                self.assertLessEqual(box[0], char['x0'])
                self.assertGreaterEqual(box[2], char['x1'])
                self.assertLessEqual(box[1], char['top'])
                self.assertGreaterEqual(box[3], char['bottom'])
        self.assertLess(box[3], 405)

    def test_overflow_fill_and_clipped_object_cannot_absorb_caption(self):
        characters = chars('Fig. 2. The caption remains outside the image.', 50, 410)
        synthetic = page(characters, images=[primitive(70, 180, 530, 390)],
                         rects=[primitive(-500, 160, 900, 600, object_type='rect', fill=True),
                                primitive(75, 180, 525, 435, object_type='rect', fill=False)])
        candidates = pdf_tools._graphic_candidates(synthetic, pdf_tools._lines(synthetic))
        self.assertTrue(candidates)
        for box in candidates:
            self.assertGreaterEqual(box[0], 50)
            self.assertLessEqual(box[2], 550)
            self.assertLess(box[3], 410)

    def test_full_width_figure_caption_reads_left_then_right(self):
        extracted = []
        left = ['Fig. 4. Left first describes the image.',
                'Left second explains the remaining data.']
        right = ['Right first continues the explanation.',
                 'Right second ends the caption text.']
        for index, (left_text, right_text) in enumerate(zip(left, right)):
            y = 400 + index * 12
            extracted.append(line(chars(left_text, 50, y, size=9,
                                        advance=242 / len(left_text))
                                  + chars(right_text, 308, y, size=9,
                                          advance=242 / len(right_text))))
        prose = line(chars('Ordinary body text has a central space but remains one line.',
                           50, 455, advance=8))
        extracted.append(prose)
        figure_boxes = [[50, 180, 550, 390]]
        split = pdf_tools._caption_columns(extracted, 600, figure_boxes)
        self.assertEqual(sum(item['text'] == prose['text'] for item in split), 1)
        self.assertEqual(len(split), 5)
        blocks = pdf_tools._ordered_blocks(split, 600, 10, figure_boxes)
        caption = ' '.join(block['text'] for block in blocks if block['bbox'][1] < 440)
        positions = [caption.index(text) for text in left + right]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(blocks[-1]['text'], prose['text'])
        merged = pdf_tools._merge_captions(blocks, figure_boxes=figure_boxes, width=600)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]['text'], ' '.join(left + right))
        self.assertEqual(merged[1]['text'], prose['text'])

    def test_column_figure_excludes_neighboring_fraction_and_short_prose(self):
        for fraction in (False, True):
            with self.subTest(fraction=fraction):
                body = chars('Stored dislocations accumulate during plastic slip.',
                             310, 85, size=9, advance=4)
                body += chars('directions as', 310, 140)
                body += chars('E = mc', 310, 190)
                labels = chars('a', 35, 95, size=8)
                labels += chars('100', 31, 220, size=8)
                caption = chars('Fig. 2. Measured fatigue response.',
                                50, 285, size=8, advance=4)
                synthetic = page(body + labels + caption,
                                 images=[primitive(50, 80, 280, 265)],
                                 rects=([primitive(410, 190, 480, 190.4)] if fraction else []))
                self.assertLess(len(synthetic.chars), 200)
                self.assertIsNone(pdf_tools._column_gutter(synthetic))
                candidates = pdf_tools._graphic_candidates(synthetic, pdf_tools._lines(synthetic))
                containing = [box for box in candidates if box[0] <= 50 and box[1] <= 80
                              and box[2] >= 280 and box[3] >= 265]
                self.assertEqual(len(containing), 1)
                box = containing[0]
                self.assertLess(box[2], 310)
                self.assertLess(box[3], 285)
                for char in labels:
                    self.assertLessEqual(box[0], char['x0'])
                    self.assertGreaterEqual(box[2], char['x1'])
                    self.assertLessEqual(box[1], char['top'])
                    self.assertGreaterEqual(box[3], char['bottom'])
                for char in body:
                    self.assertFalse(pdf_tools._inside(
                        (char['x0'] + char['x1']) / 2,
                        (char['top'] + char['bottom']) / 2, box))

    def test_caption_merge_keeps_same_height_neighboring_column_prose(self):
        caption_line = line(chars('Fig. 5. Measured response.', 50, 300,
                                  size=8, advance=8))
        prose_line = line(chars('Stored dislocations accumulate.', 310, 300,
                                size=8, advance=240 / 31))
        caption, prose = [pdf_tools._paragraphs([item], 600, 8)[0]
                          for item in (caption_line, prose_line)]
        self.assertEqual(caption['kind'], 'caption')
        self.assertEqual(prose['kind'], 'paragraph')
        merged = pdf_tools._merge_captions([caption, prose])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]['text'], caption_line['text'])
        self.assertEqual(merged[1]['text'], prose_line['text'])


if __name__ == '__main__':
    unittest.main()
