"""Task queue, cancellation and API quota pause."""
from __future__ import annotations
from contextlib import nullcontext
from .context import ApplicationContext
from .overview import generate_overview, overview_text
from .text_actions import original_text, readable_blocks
import sys
import threading


def codex_status(app: ApplicationContext, force=False):
    """旧扩展调用只收到退役状态，不能重新启动登录或订阅查询。"""
    return {'available': False, 'authenticated': False, 'retired': True,
            'message': 'Codex 登录查询已停用，请使用共享 API 配置。'}


def enqueue(app: ApplicationContext, pid, kind, options=None):
    import paper_ai
    if kind not in ('pdf_translate', 'summarize'):
        raise ValueError('作者与团队查询功能已取消。' if kind == 'team' else '此任务已取消或不受支持。')
    app.get_doc(pid)
    # 问答和切换模型均先取论文锁，避免与应用锁形成相反顺序。
    with (paper_ai.paper_lock(pid) if kind == 'summarize' else nullcontext()), app.LOCK:
        if app.SHUTDOWN.is_set():
            raise ValueError('应用正在关闭，任务未加入队列。')
        if (pid, kind) in app.QUEUED:
            if kind == 'pdf_translate' and options:
                check = app.get_doc(pid)
                app.prepare_pdf_translation(check, options)
                if check['pdf_layout_translation'].get('run_id') != app.get_doc(pid)['pdf_layout_translation'].get('run_id'):
                    raise ValueError('请先暂停当前版式翻译并等待任务停止，再开始重译。')
            if app.CANCEL.get((pid, kind), threading.Event()).is_set():
                app.RESUME_REQUESTED.add((pid, kind))
                if kind == 'pdf_translate':
                    app.update_doc(pid, lambda d: d['pdf_layout_translation'].update(status='queued', stage='等待当前版式任务暂停后继续', error='等待当前版式任务暂停后继续。'))
            return
        if kind == 'pdf_translate':
            app.update_doc(pid, lambda d: app.prepare_pdf_translation(d, options))
        elif kind == 'summarize':
            from . import api_settings
            doc = app.get_doc(pid)
            with app.db() as con:
                config = paper_ai.lock_api_config(con, pid, app.DATA, doc, app.LIBRARY)
                api_settings.get_api_key(app.DATA, config)
                generation = paper_ai.current(con, pid)['generation']
            app.update_doc(pid, lambda d: d.update(summarize_config=config, summarize_generation=generation))
        app.QUEUED.add((pid, kind))
        app.QUOTA_PAUSED.clear()  # Only an explicit user action retries after quota limits.
        app.CANCEL[(pid, kind)] = threading.Event()
        if kind == 'pdf_translate':
            app.update_doc(pid, lambda d: d['pdf_layout_translation'].update(
                status='queued', error='', error_code='', error_details={}, stage='等待本地版面分析'))
        else:
            app.update_doc(pid, lambda d: d.update(**{kind + '_status': 'queued', kind + '_error': ''}))
        app.JOBS.put((pid, kind))


def worker(app: ApplicationContext):
    import codex_bridge
    while True:
        job = app.JOBS.get()
        if job is None:
            app.JOBS.task_done()
            return
        pid, kind = job
        try:
            if app.SHUTDOWN.is_set() or kind not in ('pdf_translate', 'summarize'):
                continue
            doc = app.get_doc(pid)
            event = app.CANCEL[(pid, kind)]
            if event.is_set():
                continue
            if app.QUOTA_PAUSED.is_set():
                raise codex_bridge.TranslationError('模型服务额度或请求速率已受限，队列已暂停。恢复后请点击继续。', code='quota')
            if kind == 'pdf_translate':
                app.execute_pdf_translation(pid, event)
            elif kind == 'summarize':
                app.update_doc(pid, lambda d: d.update(summarize_status='running'))
                text = _overview_source(doc)
                import paper_ai
                from . import api_settings
                config = api_settings.resolve_config(app.DATA, doc['summarize_config'])
                result = generate_overview(app.AI_API, config, api_settings.get_api_key(app.DATA, config),
                                           text, cancel_event=event)
                if event.is_set():
                    raise codex_bridge.TranslationError('速览生成已取消，保留原有内容。', code='cancelled')
                allowed = ('title_zh', 'summary', 'overview_sections', 'tags')
                def commit(current):
                    if (current.get('deleted') or _overview_source(current) != text
                            or any(current.get(k) != doc.get(k) for k in allowed)):
                        raise ValueError('原文或速览内容已修改，未覆盖当前内容；请重新生成速览。')
                    if event.is_set() or app.SHUTDOWN.is_set():
                        raise codex_bridge.TranslationError('速览生成已取消，保留原有内容。', code='cancelled')
                    current.update(**{k: result[k] for k in allowed}, summarize_status='completed')
                app.update_doc(pid, commit)
                with app.LOCK, app.db() as con:
                    app.get_doc(pid)
                    paper_ai.add_message(con,pid,doc['summarize_generation'],'assistant','summary',
                        overview_text(result))
        except Exception as exc:
            message = str(exc)[:1800]
            limited = getattr(exc, 'code', '') == 'quota'
            if limited:
                app.QUOTA_PAUSED.set()
            try:
                if kind == 'pdf_translate':
                    from .api_diagnostics import public_details
                    paused = limited or app.CANCEL.get((pid, kind), threading.Event()).is_set() or getattr(exc, 'code', '') == 'cancelled'
                    error_code = getattr(exc, 'code', 'translation')
                    details = public_details(getattr(exc, 'details', {}))
                    app.update_doc(pid, lambda d: d['pdf_layout_translation'].update(
                        status='paused' if paused else 'error', stage='翻译已暂停' if paused else '翻译已中断',
                        error=message, error_code=error_code, error_details=details))
                else:
                    app.update_doc(pid, lambda d: d.update(**{kind + '_status': 'paused' if app.SHUTDOWN.is_set() else 'error', kind + '_error': message}))
            except Exception:
                pass
            print(f'[job {kind} {pid}] {message}', file=sys.stderr, flush=True)
        finally:
            with app.LOCK:
                app.QUEUED.discard((pid, kind))
                resume = (pid, kind) in app.RESUME_REQUESTED
                app.RESUME_REQUESTED.discard((pid, kind))
            if resume and kind in ('pdf_translate', 'summarize') and not app.QUOTA_PAUSED.is_set() and not app.SHUTDOWN.is_set():
                try:
                    app.enqueue(pid, kind)
                except ValueError:
                    pass
            app.JOBS.task_done()


def _overview_source(doc):
    return '\n\n'.join(original_text(b) for b in readable_blocks(doc)
                       if b.get('kind') != 'reference' and not b.get('is_table'))
