"""Conservative re-extraction overlays; original text and reading data survive."""
from __future__ import annotations

import copy
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import re

from .text_actions import original_text, translatable_blocks


def _key(text):
    return re.sub(r'\s+', '', text).casefold()


def _area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _intersection(a, b):
    return _area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def _coverage(block, region):
    return _intersection(block['bbox'], region['bbox']) / max(_area(block['bbox']), 1e-12)


def _split_sources(block, regions):
    """Recognize an old interleaved-column block covered by corrected sources."""
    box = block['bbox']
    if box[2] - box[0] <= .4 or box[0] >= .45 or box[2] <= .55 or block.get('translation'):
        return []
    related = [region for region in regions if region['page'] == block['page']
               and region['bbox'][2] - region['bbox'][0] < (box[2] - box[0]) * .9
               and _intersection(box, region['bbox']) > 0]
    if len(related) < 2:
        return []
    clipped = [[max(box[0], r['bbox'][0]), max(box[1], r['bbox'][1]),
                min(box[2], r['bbox'][2]), min(box[3], r['bbox'][3])] for r in related]
    edges = sorted({b[i] for b in clipped for i in (0, 2)})
    area = 0
    for left, right in zip(edges, edges[1:]):
        intervals = sorted((b[1], b[3]) for b in clipped if b[0] <= left and b[2] >= right)
        end, length = box[1], 0
        for top, bottom in intervals:
            length += max(0, bottom - max(end, top))
            end = max(end, bottom)
        area += (right - left) * length
    # Incorrect cross-column joins can truncate words at their old line break.
    tokens = lambda text: Counter(word[:3] for word in re.findall(r'[a-z0-9]{3,}', text.casefold()))
    old_words = tokens(original_text(block))
    source_text = ' '.join(region['text'] for region in related)
    new_words = tokens(source_text)
    words = re.findall(r'[a-z0-9]{3,}', original_text(block).casefold())
    if words and len(words[0]) <= 4 and any(word.endswith(words[0]) and len(word) > len(words[0]) + 2
                                          for word in re.findall(r'[a-z0-9]{3,}', source_text.casefold())):
        new_words[words[0][:3]] += 1
    matched = sum((old_words & new_words).values()) / max(sum(old_words.values()), 1)
    return related if area / max(_area(box), 1e-12) > .75 and matched > .95 else []


def _protected(doc, block):
    return bool('text_override' in block or block.get('note') or block.get('highlight')
                or block.get('translation_manually_edited')
                or any(r.get('block_id') == block['id'] for a in doc.get('text_annotations', []) for r in a.get('ranges', [])))


def _score(old, new):
    if old.get('page') != new.get('page') or not old.get('bbox'):
        return 0
    left, right = _key(original_text(old)), _key(new['text'])
    overlap = _intersection(old['bbox'], new['bbox']) / max(min(_area(old['bbox']), _area(new['bbox'])), 1e-12)
    if left == right and overlap > .1:
        return 10 + overlap
    if overlap < .35:
        return 0
    similar = SequenceMatcher(None, left, right, autojunk=False).ratio()
    if similar >= .3 or (old.get('is_formula') and new.get('is_formula')):
        return 2 + similar + overlap
    return 0


def repair_document(doc, extracted, timestamp):
    """Return a repair plan without changing the caller or discarding old IDs."""
    result = copy.deepcopy(doc)
    existing = result.get('blocks', [])
    available = {b['id']: b for b in existing}
    updated, added, hidden, warnings = [], [], [], []
    visible, source_region_ids = [], {}
    layout_fields = ('kind', 'bbox', 'is_formula', 'is_table', 'image', 'has_unparsed_math')
    for new in extracted['blocks']:
        candidates = [(score, b) for b in available.values() if (score := _score(b, new))]
        old = max(candidates, key=lambda item: item[0])[1] if candidates else None
        contributors = [b for b in available.values() if b.get('page') == new['page'] and b.get('bbox')
                        and _coverage(b, new) > .85]
        if old is not None and all(b['id'] != old['id'] for b in contributors):
            contributors.append(old)
        changed = old is not None and _key(original_text(old)) != _key(new['text'])
        if old is not None and not changed:
            contributors = [old]
        guarded = [b for b in contributors if _protected(doc, b)]
        translated = [b for b in contributors if b.get('translation', '').strip()]
        if (changed or old is None or len(contributors) > 1) and (guarded or len(translated) > 1):
            # ponytail: uncertain merged annotations/translations stay split;
            # a reviewed text revision is required before changing their scope.
            if old is not None:
                available.pop(old['id'], None)
                old.pop('extraction_hidden', None)
                visible.append(old)
            warnings.append({'page': new['page'], 'block_ids': [b['id'] for b in contributors or [old]],
                             'reason': '保留带修订、批注或多段译文的原分段，未自动合并。'})
            continue
        if old is None:
            block = copy.deepcopy(new)
            token = hashlib.sha256(f"{new['page']}:{new['bbox']}:{new['text']}".encode()).hexdigest()[:12]
            block['id'] = f"p{new['page']}-r{token}"
            if any(b['id'] == block['id'] for b in existing):
                raise ValueError('补正块编号冲突，未覆盖旧块。')
            existing.append(block)
            added.append(block['id'])
        else:
            block = old
            available.pop(old['id'], None)
            for field in layout_fields:
                block.pop(field, None)
                if field in new:
                    block[field] = copy.deepcopy(new[field])
            if changed:
                block['extraction_text'] = new['text']
            block.pop('extraction_hidden', None)
            block.pop('extraction_parent_id', None)
            updated.append(block['id'])
        visible.append(block)
        source_region_ids[new['id']] = block['id']
        for source in contributors:
            if source['id'] == block['id'] or _protected(doc, source):
                continue
            source.update(extraction_hidden=True, extraction_parent_id=block['id'])
            available.pop(source['id'], None)
            hidden.append(source['id'])
    for block in available.values():
        math_fragment = (block.get('is_formula') or len(original_text(block)) < 36
                         and len(re.findall(r'[A-Za-z]{3,}', original_text(block))) < 3)
        regions = extracted['figures'] + [new for new in extracted['blocks'] if new['id'] in source_region_ids
                  and (new.get('is_table') or new.get('is_formula') and math_fragment)]
        region = next((f for f in regions if f['page'] == block.get('page')
                       and block.get('bbox') and _coverage(block, f) > .85), None)
        if region and not _protected(doc, block):
            block.update(extraction_hidden=True, extraction_parent_id=source_region_ids.get(region['id'], region['id']))
            hidden.append(block['id'])
        elif not _protected(doc, block) and (split := _split_sources(block, [new for new in extracted['blocks']
                                                       if new['id'] in source_region_ids])):
            block.update(extraction_hidden=True, extraction_parent_id=source_region_ids[split[0]['id']])
            hidden.append(block['id'])
        elif not block.get('extraction_hidden'):
            visible.append(block)
            warnings.append({'page': block.get('page'), 'block_ids': [block['id']],
                             'reason': '未找到唯一对应的新区域，保留原块供核对。'})
    figures, unused, figure_ids = [], list(result.get('figures', [])), {}
    for figure in extracted['figures']:
        new = copy.deepcopy(figure)
        label = re.match(r'^Fig(?:ure)?\.?\s*(\d+)', new.get('caption', ''), re.I)
        candidates = []
        for old in unused:
            old_label = re.match(r'^Fig(?:ure)?\.?\s*(\d+)', old.get('caption', ''), re.I)
            if old.get('page') != new['page'] or label and old_label and label[1] != old_label[1]:
                continue
            score = _intersection(old['bbox'], new['bbox']) / max(min(_area(old['bbox']), _area(new['bbox'])), 1e-12)
            if score > .3:
                candidates.append((score + bool(label and old_label), old))
        if candidates:
            old = max(candidates, key=lambda item: item[0])[1]
            new['id'] = old['id']
            unused.remove(old)
        else:
            token = hashlib.sha256(f"{new['page']}:{new['bbox']}".encode()).hexdigest()[:12]
            new['id'] = f'fig-r{token}'
        figure_ids[figure['id']] = new['id']
        figures.append(new)
    if unused:
        result.setdefault('extraction_figure_history', []).extend(copy.deepcopy(unused))
    result['figures'] = figures
    for block in existing:
        parent = block.get('extraction_parent_id')
        if block.get('extraction_hidden') and parent in figure_ids:
            block['extraction_parent_id'] = figure_ids[parent]
    # Retain hidden sources, while ordering active sources by the new reading order.
    live_ids = {b['id'] for b in visible}
    result['blocks'] = visible + [b for b in existing if b['id'] not in live_ids]
    result['extraction'] = copy.deepcopy(extracted['extraction'])
    result['extraction']['repaired_at'] = timestamp
    report = {'updated': updated, 'added': added, 'hidden': sorted(set(hidden)), 'warnings': warnings}
    result.setdefault('extraction_repairs', []).append({'at': timestamp, **copy.deepcopy(report)})
    result['updated_at'] = timestamp
    target = translatable_blocks(result)
    if result.get('translation', {}).get('status') == 'completed' and any(not b.get('translation', '').strip() for b in target):
        result['translation'].update(status='paused', error='版面补正已保留原译文；新增或恢复的文字可继续翻译。')
    import paper_structure
    structure = paper_structure.organize(result)
    previous = doc.get('reading_structure', {})
    roles = {bid: entry['role'] for section in previous.get('sections', []) for entry in section['entries'] for bid in entry['source_ids']}
    for section in structure['sections']:
        for entry in section['entries']:
            if all(bid in roles and bid not in added for bid in entry['source_ids']):
                values = {roles[bid] for bid in entry['source_ids']}
                if len(values) == 1 and values <= {'title', 'authors', 'affiliations', 'abstract', 'article_info', 'section_heading'}:
                    entry['role'] = values.pop()
    structure.update(status='local', method='local-reextract')
    result['reading_structure'] = structure
    return result, report
