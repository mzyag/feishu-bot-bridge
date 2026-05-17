import atexit
import json
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from config import SETTINGS

_BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Dedup caches
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_SEEN_EVENT_IDS: Dict[str, float] = {}
_SEEN_MESSAGE_IDS: Dict[str, float] = {}


def _prune_seen(cache: Dict[str, float], now: float) -> None:
    ttl = SETTINGS.dedup_ttl_sec
    stale_keys = [key for key, ts in cache.items() if now - ts > ttl]
    for key in stale_keys:
        cache.pop(key, None)
    while len(cache) > SETTINGS.dedup_max_ids:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key, None)


def is_duplicate_recent(event_id: Optional[str], message_id: Optional[str]) -> bool:
    now = time.time()
    with _CACHE_LOCK:
        _prune_seen(_SEEN_EVENT_IDS, now)
        _prune_seen(_SEEN_MESSAGE_IDS, now)
        duplicated = False
        if event_id and event_id in _SEEN_EVENT_IDS:
            duplicated = True
        if message_id and message_id in _SEEN_MESSAGE_IDS:
            duplicated = True
        if event_id:
            _SEEN_EVENT_IDS[event_id] = now
        if message_id:
            _SEEN_MESSAGE_IDS[message_id] = now
        return duplicated


# ---------------------------------------------------------------------------
# Thread (Codex) state persistence
# ---------------------------------------------------------------------------

_THREAD_STATE_LOCK = threading.Lock()
_THREADS_BY_USER: Dict[str, str] = {}


def _resolve_state_file_path() -> Path:
    raw = SETTINGS.codex_thread_state_file
    path = Path(raw)
    if not path.is_absolute():
        path = _BASE_DIR / path
    return path


def _load_thread_state() -> None:
    path = _resolve_state_file_path()
    try:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        with _THREAD_STATE_LOCK:
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str) and k and v:
                    _THREADS_BY_USER[k] = v
    except Exception:
        return


def _save_thread_state() -> None:
    path = _resolve_state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_STATE_LOCK:
        payload = json.dumps(_THREADS_BY_USER, ensure_ascii=False, indent=2)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)


# ---------------------------------------------------------------------------
# Memory state persistence
# ---------------------------------------------------------------------------

_MEMORY_STATE_LOCK = threading.Lock()
_MEMORY_BY_USER: Dict[str, List[Dict[str, str]]] = {}


def _resolve_memory_file_path() -> Path:
    raw = SETTINGS.codex_memory_state_file
    path = Path(raw)
    if not path.is_absolute():
        path = _BASE_DIR / path
    return path


def _load_memory_state() -> None:
    path = _resolve_memory_file_path()
    try:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        with _MEMORY_STATE_LOCK:
            for open_id, turns in data.items():
                if not isinstance(open_id, str) or not open_id or not isinstance(turns, list):
                    continue
                cleaned_turns: List[Dict[str, str]] = []
                for turn in turns:
                    if not isinstance(turn, dict):
                        continue
                    role = str(turn.get("role", "")).strip()
                    text = str(turn.get("text", "")).strip()
                    if role in ("user", "assistant") and text:
                        cleaned_turns.append({"role": role, "text": text})
                if cleaned_turns:
                    _MEMORY_BY_USER[open_id] = cleaned_turns[-(SETTINGS.codex_memory_turns * 2) :]
    except Exception:
        return


def _save_memory_state() -> None:
    path = _resolve_memory_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _MEMORY_STATE_LOCK:
        payload = json.dumps(_MEMORY_BY_USER, ensure_ascii=False, indent=2)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)


# ---------------------------------------------------------------------------
# DebouncedSaver
# ---------------------------------------------------------------------------


class _DebouncedSaver:
    def __init__(self, fn: Callable, delay: float = 5.0):
        self._fn = fn
        self._delay = delay
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def trigger(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fn)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._fn()


_thread_saver = _DebouncedSaver(_save_thread_state)
_memory_saver = _DebouncedSaver(_save_memory_state)
atexit.register(_thread_saver.flush)
atexit.register(_memory_saver.flush)


# ---------------------------------------------------------------------------
# Thread ID accessors
# ---------------------------------------------------------------------------


def get_thread_id(open_id: str) -> Optional[str]:
    with _THREAD_STATE_LOCK:
        return _THREADS_BY_USER.get(open_id)


def set_thread_id(open_id: str, thread_id: str) -> None:
    if not open_id or not thread_id:
        return
    with _THREAD_STATE_LOCK:
        _THREADS_BY_USER[open_id] = thread_id
    _thread_saver.trigger()


def clear_thread_id(open_id: str) -> None:
    with _THREAD_STATE_LOCK:
        existed = open_id in _THREADS_BY_USER
        if existed:
            _THREADS_BY_USER.pop(open_id, None)
    if existed:
        _thread_saver.trigger()


# ---------------------------------------------------------------------------
# Memory accessors
# ---------------------------------------------------------------------------


def get_user_memory(open_id: str) -> List[Dict[str, str]]:
    if not SETTINGS.memory_enabled:
        return []
    with _MEMORY_STATE_LOCK:
        turns = _MEMORY_BY_USER.get(open_id, [])
        return [dict(x) for x in turns]


def append_user_memory(open_id: str, role: str, text: str) -> None:
    if not SETTINGS.memory_enabled:
        return
    normalized_text = (text or "").strip()
    if role not in ("user", "assistant") or not normalized_text:
        return
    with _MEMORY_STATE_LOCK:
        turns = _MEMORY_BY_USER.setdefault(open_id, [])
        turns.append({"role": role, "text": normalized_text})
        max_items = SETTINGS.codex_memory_turns * 2
        if len(turns) > max_items:
            del turns[:-max_items]
    _memory_saver.trigger()


def clear_user_memory(open_id: str) -> None:
    with _MEMORY_STATE_LOCK:
        existed = open_id in _MEMORY_BY_USER
        if existed:
            _MEMORY_BY_USER.pop(open_id, None)
    if existed:
        _memory_saver.trigger()


def format_memory_context(turns: List[Dict[str, str]]) -> str:
    if not turns:
        return ""
    lines = ["最近对话摘要（按时间顺序）:"]
    for turn in turns:
        role_label = "用户" if turn.get("role") == "user" else "助手"
        text = str(turn.get("text", "")).strip()
        if text:
            lines.append(f"- {role_label}: {text}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude session state persistence
# ---------------------------------------------------------------------------

_CLAUDE_SESSION_STATE_LOCK = threading.Lock()
_CLAUDE_SESSIONS_BY_USER: Dict[str, str] = {}


def _resolve_claude_session_state_file_path() -> Path:
    raw = SETTINGS.claude_session_state_file
    path = Path(raw)
    if not path.is_absolute():
        path = _BASE_DIR / path
    return path


def _load_claude_session_state() -> None:
    path = _resolve_claude_session_state_file_path()
    try:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        with _CLAUDE_SESSION_STATE_LOCK:
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str) and k and v:
                    _CLAUDE_SESSIONS_BY_USER[k] = v
    except Exception:
        return


def _save_claude_session_state() -> None:
    path = _resolve_claude_session_state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CLAUDE_SESSION_STATE_LOCK:
        payload = json.dumps(_CLAUDE_SESSIONS_BY_USER, ensure_ascii=False, indent=2)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(path)


def get_claude_session_id(open_id: str) -> Optional[str]:
    with _CLAUDE_SESSION_STATE_LOCK:
        return _CLAUDE_SESSIONS_BY_USER.get(open_id)


def set_claude_session_id(open_id: str, session_id: str) -> None:
    if not open_id or not session_id:
        return
    with _CLAUDE_SESSION_STATE_LOCK:
        _CLAUDE_SESSIONS_BY_USER[open_id] = session_id
    _save_claude_session_state()


def clear_claude_session_id(open_id: str) -> None:
    with _CLAUDE_SESSION_STATE_LOCK:
        existed = open_id in _CLAUDE_SESSIONS_BY_USER
        if existed:
            _CLAUDE_SESSIONS_BY_USER.pop(open_id, None)
    if existed:
        _save_claude_session_state()


# ---------------------------------------------------------------------------
# Load persisted state on import
# ---------------------------------------------------------------------------

_load_thread_state()
_load_memory_state()
_load_claude_session_state()
