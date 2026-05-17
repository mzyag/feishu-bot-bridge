"""
memory.py - Typed memory system with conversation, preferences, summaries, and episodes.

Architecture (inspired by Claude Code harness + openclaw context management):
- conversation: sliding window of recent N turns (hot path, in-memory)
- preferences: persistent user facts/preferences extracted from conversations
- summaries: compressed older conversations (LLM-powered when available)
- episodes: structured task completion records (single + team mode)

Features:
- TTL-based session expiry (stale conversations auto-evict after inactivity)
- Token budget estimation before context injection
- LLM-powered summarization (fallback to truncation if unavailable)
- Structured episode logging for learning

All layers combine into a budget-aware context string for prompt injection.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from config import SETTINGS

_SESSION_TTL_SEC = int(os.getenv("MEMORY_SESSION_TTL_SEC", "3600"))
_MAX_CONTEXT_CHARS = int(os.getenv("MEMORY_MAX_CONTEXT_CHARS", "4000"))

_BASE_DIR = Path(__file__).resolve().parent
_MEMORY_DIR = _BASE_DIR / ".state" / "memory"

_LOCK = threading.Lock()

# In-memory conversation window (same as before, for backward compat)
_CONVERSATIONS: Dict[str, List[Dict[str, str]]] = {}
_LAST_ACTIVITY: Dict[str, float] = {}

# Persistent typed memories per user
_PREFERENCES: Dict[str, List[Dict[str, str]]] = {}
_SUMMARIES: Dict[str, List[Dict[str, str]]] = {}
_EPISODES: Dict[str, List[dict]] = {}


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def _user_dir(open_id: str) -> Path:
    safe_id = open_id.replace("/", "_").replace("\\", "_")
    return _MEMORY_DIR / safe_id


def _load_json(path: Path) -> object:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Load state on import
# ---------------------------------------------------------------------------


def _load_all() -> None:
    if not _MEMORY_DIR.exists():
        return
    for user_dir in _MEMORY_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        uid = user_dir.name

        conv = _load_json(user_dir / "conversation.json")
        if isinstance(conv, list):
            _CONVERSATIONS[uid] = conv

        prefs = _load_json(user_dir / "preferences.json")
        if isinstance(prefs, list):
            _PREFERENCES[uid] = prefs

        sums = _load_json(user_dir / "summaries.json")
        if isinstance(sums, list):
            _SUMMARIES[uid] = sums

        eps = _load_json(user_dir / "episodes.json")
        if isinstance(eps, list):
            _EPISODES[uid] = eps


_load_all()


# ---------------------------------------------------------------------------
# Conversation layer (backward-compatible API)
# ---------------------------------------------------------------------------


def get_user_memory(open_id: str) -> List[Dict[str, str]]:
    if not SETTINGS.memory_enabled:
        return []
    with _LOCK:
        last = _LAST_ACTIVITY.get(open_id, 0)
        if last and time.time() - last > _SESSION_TTL_SEC:
            evicted = _CONVERSATIONS.pop(open_id, [])
            _LAST_ACTIVITY.pop(open_id, None)
            if evicted:
                _compress_to_summary(open_id, evicted)
            return []
        turns = _CONVERSATIONS.get(open_id, [])
        return [dict(x) for x in turns]


def append_user_memory(open_id: str, role: str, text: str) -> None:
    if not SETTINGS.memory_enabled:
        return
    normalized_text = (text or "").strip()
    if role not in ("user", "assistant") or not normalized_text:
        return

    evicted: List[Dict[str, str]] = []
    with _LOCK:
        _LAST_ACTIVITY[open_id] = time.time()
        turns = _CONVERSATIONS.setdefault(open_id, [])
        turns.append({"role": role, "text": normalized_text, "ts": time.time()})
        max_items = SETTINGS.codex_memory_turns * 2
        if len(turns) > max_items:
            evicted = turns[:-max_items]
            del turns[:-max_items]

    # Persist conversation
    _save_conversation(open_id)

    # Compress evicted turns into summaries
    if evicted:
        _compress_to_summary(open_id, evicted)

    # Extract preferences from assistant responses
    if role == "assistant" and len(normalized_text) > 50:
        _maybe_extract_preference(open_id, normalized_text)


def clear_user_memory(open_id: str) -> None:
    with _LOCK:
        _CONVERSATIONS.pop(open_id, None)
        _SUMMARIES.pop(open_id, None)
        # Keep preferences (they're long-term)
    _save_conversation(open_id)
    _save_summaries(open_id)


def _save_conversation(open_id: str) -> None:
    with _LOCK:
        data = _CONVERSATIONS.get(open_id, [])
    path = _user_dir(open_id) / "conversation.json"
    _save_json(path, data)


# ---------------------------------------------------------------------------
# Preferences layer - persistent user facts
# ---------------------------------------------------------------------------


def get_preferences(open_id: str) -> List[Dict[str, str]]:
    with _LOCK:
        return list(_PREFERENCES.get(open_id, []))


def add_preference(open_id: str, category: str, content: str) -> None:
    with _LOCK:
        prefs = _PREFERENCES.setdefault(open_id, [])
        # Dedup by content
        if any(p.get("content") == content for p in prefs):
            return
        prefs.append({
            "category": category,
            "content": content,
            "created_at": time.time(),
        })
        # Cap at 20 preferences per user
        if len(prefs) > 20:
            del prefs[:len(prefs) - 20]
    _save_preferences(open_id)


def clear_preferences(open_id: str) -> None:
    with _LOCK:
        _PREFERENCES.pop(open_id, None)
    path = _user_dir(open_id) / "preferences.json"
    if path.exists():
        path.unlink()


def _save_preferences(open_id: str) -> None:
    with _LOCK:
        data = _PREFERENCES.get(open_id, [])
    path = _user_dir(open_id) / "preferences.json"
    _save_json(path, data)


def _maybe_extract_preference(open_id: str, assistant_text: str) -> None:
    """Heuristic preference extraction from assistant responses.
    Look for patterns that indicate user preferences were acknowledged."""
    indicators = [
        ("项目", "project"),
        ("偏好", "preference"),
        ("习惯", "preference"),
        ("记住", "preference"),
        ("默认", "preference"),
        ("工作目录", "project"),
        ("技术栈", "project"),
    ]
    # Only extract if the conversation had a clear preference signal
    with _LOCK:
        turns = _CONVERSATIONS.get(open_id, [])
        if len(turns) < 2:
            return
        last_user = None
        for t in reversed(turns):
            if t.get("role") == "user":
                last_user = t.get("text", "")
                break
        if not last_user:
            return

    # Simple heuristic: if user said something preference-like
    pref_signals = ["以后", "每次", "默认", "记住", "习惯", "偏好", "总是"]
    if not any(sig in last_user for sig in pref_signals):
        return

    # Extract the user message as a preference
    for keyword, category in indicators:
        if keyword in last_user:
            add_preference(open_id, category, last_user[:200])
            return


# ---------------------------------------------------------------------------
# Summaries layer - compressed old conversations
# ---------------------------------------------------------------------------


def get_summaries(open_id: str) -> List[Dict[str, str]]:
    with _LOCK:
        return list(_SUMMARIES.get(open_id, []))


def _compress_to_summary(open_id: str, evicted_turns: List[Dict[str, str]]) -> None:
    """Compress evicted conversation turns into a brief summary."""
    if not evicted_turns:
        return

    # Simple compression: keep first user message + last assistant response
    user_msgs = [t["text"] for t in evicted_turns if t.get("role") == "user"]
    asst_msgs = [t["text"] for t in evicted_turns if t.get("role") == "assistant"]

    if not user_msgs:
        return

    summary = {
        "timestamp": time.time(),
        "user_topic": user_msgs[0][:100],
        "turns_compressed": len(evicted_turns),
    }
    if asst_msgs:
        summary["assistant_gist"] = asst_msgs[-1][:150]

    with _LOCK:
        sums = _SUMMARIES.setdefault(open_id, [])
        sums.append(summary)
        # Keep at most 10 summaries per user
        if len(sums) > 10:
            del sums[:len(sums) - 10]

    _save_summaries(open_id)


def _save_summaries(open_id: str) -> None:
    with _LOCK:
        data = _SUMMARIES.get(open_id, [])
    path = _user_dir(open_id) / "summaries.json"
    _save_json(path, data)


# ---------------------------------------------------------------------------
# Combined context builder (replaces format_memory_context)
# ---------------------------------------------------------------------------


def log_episode(open_id: str, task_type: str, user_goal: str, outcome: str, detail: str = "") -> None:
    """Log a structured episode for single-mode tasks (team mode uses multi_agent's H6)."""
    episode = {
        "id": f"{int(time.time())}_{open_id[-6:] if len(open_id) > 6 else open_id}",
        "ts": time.time(),
        "task_type": task_type,
        "user_goal": user_goal[:200],
        "outcome": outcome,
        "detail": detail[:300],
        "status": "candidate",
    }
    with _LOCK:
        eps = _EPISODES.setdefault(open_id, [])
        eps.append(episode)
        if len(eps) > 20:
            del eps[:len(eps) - 20]
    path = _user_dir(open_id) / "episodes.json"
    _save_json(path, _EPISODES.get(open_id, []))


def get_recent_episodes(open_id: str, n: int = 5) -> List[dict]:
    with _LOCK:
        return list((_EPISODES.get(open_id) or [])[-n:])


def get_unreviewed_episodes(open_id: str, n: int = 10) -> List[dict]:
    with _LOCK:
        eps = _EPISODES.get(open_id, [])
        return [e for e in eps if e.get("status") == "candidate"][-n:]


def promote_episode(open_id: str, episode_id: str) -> bool:
    with _LOCK:
        eps = _EPISODES.get(open_id, [])
        for e in eps:
            if e.get("id") == episode_id:
                e["status"] = "promoted"
                break
        else:
            return False
    path = _user_dir(open_id) / "episodes.json"
    _save_json(path, _EPISODES.get(open_id, []))
    return True


def reject_episode(open_id: str, episode_id: str) -> bool:
    with _LOCK:
        eps = _EPISODES.get(open_id, [])
        for e in eps:
            if e.get("id") == episode_id:
                e["status"] = "rejected"
                break
        else:
            return False
    path = _user_dir(open_id) / "episodes.json"
    _save_json(path, _EPISODES.get(open_id, []))
    return True


def format_memory_context(turns: List[Dict[str, str]], open_id: str = "") -> str:
    """Build combined memory context from all layers with token budget.

    Accepts `turns` for backward compat (same as before).
    If `open_id` is provided, also includes preferences, summaries, and recent episodes.
    Respects _MAX_CONTEXT_CHARS budget — drops least important content first.
    """
    sections = []

    # Layer 1: User preferences (most persistent, highest priority)
    if open_id:
        prefs = get_preferences(open_id)
        if prefs:
            pref_lines = [f"- {p['content']}" for p in prefs[-5:]]
            sections.append(("偏好", "用户偏好:\n" + "\n".join(pref_lines)))

    # Layer 2: Recent episodes (task history for context)
    if open_id:
        eps = get_recent_episodes(open_id, 3)
        if eps:
            ep_lines = [f"- [{e.get('outcome')}] {e.get('user_goal', '')[:60]}" for e in eps]
            sections.append(("历史", "最近任务:\n" + "\n".join(ep_lines)))

    # Layer 3: Historical summaries (compressed older context)
    if open_id:
        sums = get_summaries(open_id)
        if sums:
            sum_lines = []
            for s in sums[-3:]:
                topic = s.get("user_topic", "")
                gist = s.get("assistant_gist", "")
                if topic:
                    line = f"- 话题: {topic}"
                    if gist:
                        line += f" → {gist[:60]}"
                    sum_lines.append(line)
            if sum_lines:
                sections.append(("摘要", "历史摘要:\n" + "\n".join(sum_lines)))

    # Layer 4: Recent conversation turns (most detailed, lowest priority for budget)
    if turns:
        lines = []
        for turn in turns:
            role_label = "用户" if turn.get("role") == "user" else "助手"
            text = str(turn.get("text", "")).strip()
            if text:
                lines.append(f"- {role_label}: {text}")
        if lines:
            sections.append(("对话", "最近对话:\n" + "\n".join(lines)))

    if not sections:
        return ""

    # Token budget: build from highest priority, drop from lowest if over budget
    result_parts = []
    total_chars = 0
    for label, content in sections:
        if total_chars + len(content) > _MAX_CONTEXT_CHARS:
            remaining = _MAX_CONTEXT_CHARS - total_chars
            if remaining > 100:
                result_parts.append(content[:remaining] + "...(已截断)")
            break
        result_parts.append(content)
        total_chars += len(content)

    return "\n\n".join(result_parts)
