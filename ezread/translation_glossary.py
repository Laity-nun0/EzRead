"""每篇论文独立、可由缓存顺序重放的保守术语表。"""
import json
import re

MAX_ENTRIES = 500
MAX_TERM_LENGTH = 160
MAX_TERM_WORDS = 16
MAX_WARNINGS = 100


def _entry(value):
    if not isinstance(value, dict):
        raise ValueError('术语条目必须包含原文 source 和译文 target。')
    result = {}
    for name in ('source', 'target'):
        text = value.get(name)
        if not isinstance(text, str) or not text.strip():
            raise ValueError('术语原文和译文必须是非空文字。')
        text = ' '.join(text.split())
        if len(text) > MAX_TERM_LENGTH or len(text.split()) > MAX_TERM_WORDS:
            raise ValueError(f'单条术语最多 {MAX_TERM_LENGTH} 个字符、{MAX_TERM_WORDS} 个词。')
        result[name] = text
    return result


def validate_seed(value):
    """人工明确提供或继承的种子；不推断词形或缩写含义。"""
    if not isinstance(value, list):
        raise ValueError('术语表必须是列表。')
    if len(value) > MAX_ENTRIES:
        raise ValueError(f'术语表最多 {MAX_ENTRIES} 条。')
    entries = {}
    for item in value:
        term = _entry(item)
        source, target = term['source'], term['target']
        if source in entries and entries[source] != target:
            raise ValueError(f'术语“{source}”存在冲突译法。')
        entries[source] = target
    return [{'source': source, 'target': target} for source, target in entries.items()]


def validate_native_seed(value):
    """原生 Glossary 不区分大小写并直接生成表格；拒绝会静默丢项的输入。"""
    entries = validate_seed(value)
    normalized = {}
    for entry in entries:
        source, target = entry['source'], entry['target']
        if '|' in source or '|' in target:
            raise ValueError('原生术语表暂不支持含竖线的术语，请移除该条目中的竖线。')
        key = source.lower()
        if key in normalized and normalized[key] != target:
            raise ValueError(f'原生术语表不区分大小写，术语“{source}”存在冲突译法。')
        normalized[key] = target
    return entries


def _contains(text, term):
    text = ' '.join(text.split())
    # 拉丁词形严格匹配；中文可紧邻术语，例如“铝Al合金”。不合并 Al/AL/al。
    pattern = re.escape(term)
    if term[0].isascii() and (term[0].isalnum() or term[0] == '_'):
        pattern = r'(?<![A-Za-z0-9_])' + pattern
    if term[-1].isascii() and (term[-1].isalnum() or term[-1] == '_'):
        pattern += r'(?![A-Za-z0-9_])'
    return re.search(pattern, text) is not None


class PaperGlossary:
    def __init__(self, seed=None):
        self._entries = {item['source']: {**item, 'origin': 'user'}
                         for item in validate_seed([] if seed is None else seed)}
        self.warnings = []

    def _warn(self, message):
        if message not in self.warnings and len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(message)

    def context(self, units):
        return [{name: entry[name] for name in ('source', 'target')}
                for entry in self._entries.values()
                if any(_contains(unit['input'], entry['source']) for unit in units)]

    def accept(self, units, response, terms, *, batch=True):
        """只接收同一单元内能核对的原文与译文；术语错误不影响正式译文。"""
        try:
            values = {item['id']: item['output'] for item in json.loads(response)} if batch else {0: response}
        except (ValueError, TypeError, KeyError):
            self._warn('译文单元无法核对，未采用模型术语。')
            return
        for entry in self._entries.values():
            if any(_contains(unit['input'], entry['source'])
                   and not _contains(values.get(unit['id'], ''), entry['target']) for unit in units):
                self._warn(f'术语“{entry["source"]}”的既定译法“{entry["target"]}”未在对应译文中找到，请核对。')
        if terms is None:
            return
        if not isinstance(terms, list):
            self._warn('模型术语格式无效，已忽略；译文仍保留。')
            return
        if len(terms) > MAX_ENTRIES:
            self._warn(f'模型术语超过 {MAX_ENTRIES} 条，超出部分已忽略。')
        for item in terms[:MAX_ENTRIES]:
            try:
                entry = _entry(item)
            except ValueError as exc:
                self._warn(f'模型术语格式无效，已忽略：{exc}')
                continue
            source, target = entry['source'], entry['target']
            if not any(_contains(unit['input'], source)
                       and isinstance(values.get(unit['id']), str)
                       and _contains(values[unit['id']], target) for unit in units):
                self._warn(f'模型术语“{source}”未在同一原译文单元核实，已忽略。')
                continue
            previous = self._entries.get(source)
            if previous:
                if previous['target'] != target:
                    self._warn(f'术语“{source}”译法冲突，保留“{previous["target"]}”，未采用“{target}”。')
                continue
            if len(self._entries) >= MAX_ENTRIES:
                self._warn(f'术语表已达 {MAX_ENTRIES} 条上限，额外术语未采用。')
                continue
            self._entries[source] = {**entry, 'origin': 'model'}

    def snapshot(self):
        return {'entries': [dict(entry) for entry in self._entries.values()], 'warnings': list(self.warnings)}
