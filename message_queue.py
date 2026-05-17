"""
Unified message task queue for feishu-bot-bridge.

All messaging platforms (Feishu, WeChat, future adapters) enqueue tasks here.
The queue handles worker dispatch, same-user message superseding, and task
lifecycle tracking. Completed tasks are automatically removed from the queue.

Usage for any platform adapter:
    from message_queue import message_queue, MessageTask

    message_queue.enqueue(MessageTask(
        source="feishu",
        user_id=open_id,
        text=user_text,
        reply_fn=my_reply_function,
        generate_reply_fn=shared_generate_reply,
    ))
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class MessageTask:
    source: str
    user_id: str
    text: str
    reply_fn: Callable[[str], None]
    generate_reply_fn: Callable
    on_progress: Optional[Callable[[str, str], None]] = None
    ack_text: str = "收到，开始执行..."
    seq: int = 0
    enqueued_at: float = field(default_factory=time.time)


@dataclass
class _TaskEntry:
    task: MessageTask
    stage: str = "排队中"
    detail: str = ""
    started_at: float = 0.0
    done: bool = False
    ok: bool = False
    last_updated_at: float = field(default_factory=time.time)


class MessageQueue:
    """
    Platform-agnostic message task queue.

    - enqueue(): accept tasks from any channel
    - Automatically cancels a user's previous running task when a new one arrives
    - Workers process tasks and clean them from the queue upon completion
    - Trace log for observability
    """

    def __init__(self, max_workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="msg-queue-worker")
        self._lock = threading.Lock()
        self._seq_by_user: Dict[str, int] = {}
        self._cancel_events: Dict[str, Tuple[int, threading.Event]] = {}
        self._active_tasks: Dict[str, _TaskEntry] = {}
        self._last_completed: Dict[str, _TaskEntry] = {}
        self._trace_log: Dict[str, List[Tuple[float, int, str, str, str]]] = {}
        self._trace_max_per_user = 50

    def enqueue(self, task: MessageTask) -> int:
        with self._lock:
            seq = self._seq_by_user.get(task.user_id, 0) + 1
            self._seq_by_user[task.user_id] = seq
            task.seq = seq

            current_cancel = self._cancel_events.get(task.user_id)
            if current_cancel:
                current_cancel[1].set()

            active = self._active_tasks.get(task.user_id)
            if active and not active.done:
                active.stage = "正在取消旧任务"
                active.detail = "收到新的用户消息"
                active.last_updated_at = time.time()

        self._trace(task.source, task.user_id, seq, "入队", _preview(task.text, 140))

        try:
            self._pool.submit(self._worker, task)
        except Exception:
            task.reply_fn("当前消息队列���常，请���后重试。")

        return seq

    def get_status(self, user_id: str) -> Tuple[Optional[_TaskEntry], Optional[_TaskEntry]]:
        with self._lock:
            return self._active_tasks.get(user_id), self._last_completed.get(user_id)

    def get_trace(self, user_id: str, last_n: int = 20) -> List[Tuple[float, int, str, str, str]]:
        with self._lock:
            entries = self._trace_log.get(user_id, [])
            return entries[-last_n:]

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._active_tasks.values() if not e.done)

    def _is_latest(self, user_id: str, seq: int) -> bool:
        with self._lock:
            return self._seq_by_user.get(user_id, 0) == seq

    def _worker(self, task: MessageTask) -> None:
        if not self._is_latest(task.user_id, task.seq):
            self._trace(task.source, task.user_id, task.seq, "跳过", "已被新消息覆盖")
            return

        cancel_event = threading.Event()
        now = time.time()
        entry = _TaskEntry(task=task, stage="执行中", started_at=now, last_updated_at=now)

        with self._lock:
            self._active_tasks[task.user_id] = entry
            self._cancel_events[task.user_id] = (task.seq, cancel_event)

        self._trace(task.source, task.user_id, task.seq, "开始", f"[{task.source}] {_preview(task.text)}")

        if task.ack_text:
            task.reply_fn(task.ack_text)

        # Heartbeat: send periodic "still processing" updates
        heartbeat_stop = threading.Event()

        def _heartbeat():
            while not heartbeat_stop.wait(30):
                if entry.done:
                    return
            print(f"[heartbeat] exiting: stop event set")

        heartbeat_thread = threading.Thread(target=_heartbeat, name=f"heartbeat-{task.seq}", daemon=True)
        heartbeat_thread.start()

        def _progress(stage: str, detail: str = "") -> None:
            with self._lock:
                e = self._active_tasks.get(task.user_id)
                if e and e.task.seq == task.seq:
                    e.stage = stage
                    e.detail = _preview(detail, 160)
                    e.last_updated_at = time.time()
            if task.on_progress:
                task.on_progress(stage, detail)

        started = time.time()
        try:
            reply_result = task.generate_reply_fn(
                task.text,
                task.user_id,
                progress_callback=_progress,
                cancel_event=cancel_event,
            )
        except Exception:
            reply_result = _ErrorResult()

        elapsed = time.time() - started
        heartbeat_stop.set()

        with self._lock:
            self._cancel_events.pop(task.user_id, None)

        if not self._is_latest(task.user_id, task.seq):
            print(f"[queue] DROPPED reply (not latest): seq={task.seq}, reply[:60]={reply_result.reply[:60]}")
            self._finish(task.source, task.user_id, task.seq, ok=False, stage="已被新消息覆盖")
            return

        self._finish(
            task.source, task.user_id, task.seq,
            ok=reply_result.ok,
            stage="已完成" if reply_result.ok else "执行失败",
            detail=f"{reply_result.status}: {reply_result.reply[:120]}",
        )
        print(f"[queue] SENDING final reply: seq={task.seq}, len={len(reply_result.reply)}")
        try:
            task.reply_fn(reply_result.reply)
            print(f"[queue] reply sent OK")
        except Exception as ex:
            print(f"[queue] reply FAILED: {ex}")

    def _finish(self, source: str, user_id: str, seq: int, ok: bool, stage: str, detail: str = "") -> None:
        with self._lock:
            entry = self._active_tasks.pop(user_id, None)
            if entry and entry.task.seq == seq:
                entry.done = True
                entry.ok = ok
                entry.stage = stage
                entry.detail = _preview(detail, 160)
                entry.last_updated_at = time.time()
                self._last_completed[user_id] = entry
        self._trace(source, user_id, seq, "完成" if ok else "失败", f"{stage} {detail[:60]}")

    def _trace(self, source: str, user_id: str, seq: int, event: str, detail: str) -> None:
        with self._lock:
            entries = self._trace_log.setdefault(user_id, [])
            entries.append((time.time(), seq, source, event, detail))
            if len(entries) > self._trace_max_per_user:
                self._trace_log[user_id] = entries[-self._trace_max_per_user:]

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)


class _ErrorResult:
    ok = False
    reply = "处理消息时发生异常，请稍后重试。"
    status = "worker_exception"


def _preview(text: str, limit: int = 80) -> str:
    normalized = " ".join((text or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


message_queue = MessageQueue(max_workers=4)
