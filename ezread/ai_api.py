"""应用内 AI HTTP 会话池；按通道串行复用连接，不保存论文或对话历史。"""

import threading

from codex_bridge import TranslationError
from ezread.translation_api import ApiTranslationSession


class ApiSessionPool:
    """由应用显式持有并关闭；导入和实例化均不连接远端。"""

    def __init__(self, usage=None):
        self._usage = usage
        self._closed = threading.Event()
        self._state_lock = threading.Lock()
        self._channel_locks = {name: threading.Lock() for name in (
            'chat', 'overview', 'structure', 'selection', 'retranslate', 'journal')}
        self._sessions = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check_cancelled(self, cancel_event):
        if self._closed.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise TranslationError('AI 请求已取消或通道池已关闭。', retryable=False, code='cancelled')

    def request_json(self, channel, config, api_key, system, prompt, *, cancel_event=None):
        """同通道等待可取消；配置和密钥变化后关闭旧连接，下一次请求不自动重试。"""
        if channel not in self._channel_locks:
            raise TranslationError('AI 请求通道无效。', retryable=False, code='configuration')
        # 请求进入时固定配置，避免等待通道期间外部修改同一个字典。
        config = dict(config)
        channel_lock = self._channel_locks[channel]
        self._check_cancelled(cancel_event)
        while not channel_lock.acquire(timeout=0.05):
            self._check_cancelled(cancel_event)
        try:
            self._check_cancelled(cancel_event)
            with self._state_lock:
                self._check_cancelled(cancel_event)
                entry = self._sessions.get(channel)
                if entry is None or entry[0] != config or entry[1] != api_key:
                    if entry is not None:
                        entry[2].close()
                        del self._sessions[channel]
                    session = ApiTranslationSession(config, api_key, usage=self._usage, channel=channel)
                    self._sessions[channel] = (config, api_key, session)
                else:
                    session = entry[2]
            return session.request_json(system, prompt, cancel_event=cancel_event)
        finally:
            channel_lock.release()

    def close(self):
        """不等待串行锁，立即取消所有通道的在途请求并唤醒等待者。"""
        with self._state_lock:
            self._closed.set()
            sessions = [entry[2] for entry in self._sessions.values()]
            self._sessions.clear()
        for session in sessions:
            session.close()
