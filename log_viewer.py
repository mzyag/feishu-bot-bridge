"""
log_viewer.py - 桌面 Codex 日志解析、session 事件格式化、trace 追踪、用户状态格式化
依赖: config, state, text_utils, message_queue
"""

import calendar
import json
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import lark_oapi as lark

from config import SETTINGS
from text_utils import (
    preview_text,
    redact_log_text,
    requested_log_lines,
    requested_session_entries,
    requested_trace_entries,
    wants_codex_session_logs,
    wants_extension_logs,
)

_BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Desktop Codex log / session path discovery
# ---------------------------------------------------------------------------


def _latest_codex_desktop_log_path() -> Optional[Path]:
    roots = [
        Path.home() / "Library/Application Support/Code/logs",
        Path.home() / "Library/Application Support/Cursor/logs",
    ]
    candidates: List[Tuple[float, Path]] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for path in root.glob("*/window*/exthost/openai.chatgpt/Codex.log"):
                try:
                    candidates.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _is_feishu_bot_codex_session(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            checked = 0
            for raw in fh:
                checked += 1
                if checked > 80:
                    break
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "user_message":
                    continue
                message = str(payload.get("message") or "")
                if "你是飞书里的中文助手" in message and "工程目录约束" in message:
                    return True
    except OSError:
        return False
    return False


def _latest_codex_session_path(include_feishu_bot: bool = False) -> Optional[Path]:
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.exists():
        return None
    candidates: List[Tuple[float, Path]] = []
    try:
        for path in sessions_root.glob("**/rollout-*.jsonl"):
            if not include_feishu_bot and _is_feishu_bot_codex_session(path):
                continue
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


# ---------------------------------------------------------------------------
# Session event text helpers
# ---------------------------------------------------------------------------


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        value = item.get("text") or item.get("input_text") or item.get("output_text")
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def _short_session_path(path: Path) -> str:
    parts = path.parts
    try:
        idx = parts.index(".codex")
        rel = "/".join(parts[idx:])
    except ValueError:
        rel = path.name
    if len(rel) <= 72:
        return rel
    return f"{rel[:34]}...{rel[-34:]}"


def _clean_session_message(message: str) -> str:
    text = message.strip()
    marker = "## My request for Codex:"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    transcript_match = re.search(r"最近\s+\d+\s*条\s+Codex\s+桌面任务记录", text)
    if transcript_match:
        text = text[: transcript_match.start()].rstrip("：: \n")
    text = re.sub(r"<image\b.*?</image>", "[图片]", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<image\b[^>]*>", "[图片]", text, flags=re.IGNORECASE)
    text = re.sub(r"```[a-zA-Z0-9_-]*", "", text)
    text = text.replace("```", "")
    text = re.sub(r"/Users/[^\s`，。；、)）]+", "<本地路径>", text)
    text = re.sub(r"…s/[^\s`，。；、)）]+", "<截断路径>", text)
    return " ".join(text.split())


def _summarize_shell_command(command_text: str) -> str:
    command = " ".join(command_text.strip().split())
    command = command.replace(str(Path.home()), "~")
    command = command.replace('"$HOME', '"~').replace("'$HOME", "'~").replace("$HOME", "~")
    command = re.sub(r"/Users/[^\s\"']+", "<本地路径>", command)
    command = re.sub(r"\s*2>/dev/null", "", command)
    if command.startswith("/bin/zsh -lc "):
        command = command[len("/bin/zsh -lc "):]
    if command.startswith("python3 - <<"):
        return "python3 inline script"
    if command.startswith("python3 /Users/") or command.startswith("python3 <本地路径>"):
        return preview_text(command, 76)
    if command.startswith("find "):
        pieces = command.split("|", 1)
        return preview_text(pieces[0], 76)
    if command.startswith("rg "):
        return preview_text(command, 76)
    return preview_text(command, 76)


def _session_event_block(ts_label: str, title: str, body: str = "") -> str:
    if body:
        return f"{ts_label} {title}\n  {body}"
    return f"{ts_label} {title}"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _format_elapsed_since(epoch_seconds: float) -> str:
    delta = max(0, int(time.time() - epoch_seconds))
    if delta < 60:
        return f"{delta}s前"
    if delta < 3600:
        return f"{delta // 60}m{delta % 60}s前"
    return f"{delta // 3600}h{(delta % 3600) // 60}m前"


def _parse_iso_epoch(timestamp: str) -> Optional[float]:
    if not timestamp:
        return None
    try:
        return float(calendar.timegm(time.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return None


def _clock_label(epoch_seconds: Optional[float], fallback_timestamp: str = "") -> str:
    if epoch_seconds is not None:
        return time.strftime("%H:%M:%S", time.localtime(epoch_seconds))
    return fallback_timestamp[11:19] if len(fallback_timestamp) >= 19 else "--:--:--"


# ---------------------------------------------------------------------------
# Desktop stage detection
# ---------------------------------------------------------------------------


def _desktop_stage_from_event(item_type: str, payload_type: str, payload: dict) -> Tuple[str, str]:
    if item_type == "event_msg":
        if payload_type == "task_started":
            return "任务已开始", ""
        if payload_type == "task_complete":
            return "任务已完成", ""
        if payload_type == "agent_message":
            return "已输出可见进展", _clean_session_message(str(payload.get("message") or ""))
        if payload_type == "exec_command_end":
            command = payload.get("command")
            if isinstance(command, list):
                command_text = " ".join(str(part) for part in command[-3:])
            else:
                command_text = str(command or "")
            exit_code = payload.get("exit_code")
            status = "OK" if exit_code == 0 else f"FAIL {exit_code}"
            return f"工具执行完成 {status}", _summarize_shell_command(command_text)
        if payload_type == "patch_apply_begin":
            return "正在修改文件", ""
        if payload_type == "patch_apply_end":
            return "文件修改已完成", ""
    if item_type == "response_item":
        if payload_type == "reasoning":
            return "模型正在处理", ""
        if payload_type == "function_call":
            name = str(payload.get("name") or "tool")
            return "正在准备工具调用", name
        if payload_type == "function_call_output":
            return "工具结果已返回", ""
        if payload_type == "message":
            role = str(payload.get("role") or "")
            if role == "assistant":
                return "正在整理回复", ""
    return "", ""


# ---------------------------------------------------------------------------
# Desktop Codex status formatting
# ---------------------------------------------------------------------------


def format_desktop_codex_status(user_text: str = "") -> str:
    session_path = _latest_codex_session_path(include_feishu_bot=False)
    if session_path is None:
        return "未找到桌面 Codex 会话记录：~/.codex/sessions/**/rollout-*.jsonl"

    try:
        raw_lines = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as ex:
        return f"读取桌面 Codex 会话失败：{type(ex).__name__}: {ex}"

    last_task_started_idx = -1
    last_task_complete_idx = -1
    last_task_started_ts = ""
    last_task_started_epoch: Optional[float] = None
    last_activity_ts = ""
    last_activity_epoch = session_path.stat().st_mtime
    user_message = ""
    stage = "未知"
    detail = ""
    recent_events: List[str] = []

    parsed_items: List[Tuple[int, dict]] = []
    for idx, raw in enumerate(raw_lines):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        parsed_items.append((idx, item))
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        item_type = str(item.get("type") or "")
        payload_type = str(payload.get("type") or "")
        timestamp = str(item.get("timestamp") or "")
        if timestamp:
            last_activity_ts = timestamp
            parsed_epoch = _parse_iso_epoch(timestamp)
            if parsed_epoch is not None:
                last_activity_epoch = parsed_epoch
        if item_type == "event_msg" and payload_type == "task_started":
            last_task_started_idx = idx
            last_task_started_ts = timestamp
            last_task_started_epoch = _parse_iso_epoch(timestamp)
            user_message = ""
            stage = "任务已开始"
            detail = ""
            recent_events = []
            continue
        if item_type == "event_msg" and payload_type == "task_complete":
            last_task_complete_idx = idx
        if last_task_started_idx < 0 or idx < last_task_started_idx:
            continue
        if item_type == "event_msg" and payload_type == "user_message":
            cleaned = _clean_session_message(str(payload.get("message") or ""))
            if cleaned and not user_message:
                user_message = cleaned
            continue
        next_stage, next_detail = _desktop_stage_from_event(item_type, payload_type, payload)
        if next_stage:
            stage = next_stage
            detail = next_detail
            ts_label = _clock_label(_parse_iso_epoch(timestamp), timestamp)
            if next_detail:
                recent_events.append(_session_event_block(ts_label, next_stage, preview_text(next_detail, 110)))
            else:
                recent_events.append(_session_event_block(ts_label, next_stage))

    running = last_task_started_idx >= 0 and last_task_started_idx > last_task_complete_idx
    state_label = "运行中" if running else "最近任务已完成"
    if not user_message:
        user_message = "（未解析到用户消息）"
    if last_task_complete_idx >= last_task_started_idx >= 0:
        stage = "任务已完成"

    header = [
        "桌面 Codex 任务状态",
        f"状态: {state_label}",
        f"任务: {preview_text(user_message, 120)}",
        f"阶段: {stage}",
        f"最后活动: {_clock_label(last_activity_epoch, last_activity_ts)}（{_format_elapsed_since(last_activity_epoch)}）",
        f"会话: {_short_session_path(session_path)}",
    ]
    if last_task_started_ts:
        header.insert(3, f"开始: {_clock_label(last_task_started_epoch, last_task_started_ts)}")
    if detail:
        header.append(f"详情: {redact_log_text(preview_text(detail, 160))}")

    events = [redact_log_text(event) for event in recent_events[-6:]]
    if events:
        return "\n".join(header) + "\n\n最近事件:\n" + "\n\n".join(events) + "\n\n发送 `桌面日志 10条` 可查看可见 transcript。"
    return "\n".join(header) + "\n\n发送 `桌面日志 10条` 可查看可见 transcript。"


# ---------------------------------------------------------------------------
# Codex session events formatting
# ---------------------------------------------------------------------------


def _format_codex_session_events(session_path: Path, entry_count: int) -> Tuple[List[str], int]:
    events: List[str] = []
    try:
        raw_lines = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as ex:
        return [f"读取 Codex 桌面任务记录失败：{type(ex).__name__}: {ex}"], 0

    for raw in raw_lines:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        timestamp = str(item.get("timestamp") or "")
        ts_label = timestamp[11:19] if len(timestamp) >= 19 else "--:--:--"
        event_type = payload.get("type")
        if item.get("type") == "event_msg" and event_type in {"user_message", "agent_message"}:
            role = "用户" if event_type == "user_message" else "助手"
            message = _clean_session_message(str(payload.get("message") or ""))
            if message:
                events.append(_session_event_block(ts_label, role, preview_text(message, 180)))
            continue
        if item.get("type") == "event_msg" and event_type == "exec_command_end":
            command = payload.get("command")
            if isinstance(command, list):
                command_text = " ".join(str(part) for part in command[-3:])
            else:
                command_text = str(command or "")
            exit_code = payload.get("exit_code")
            status = "OK" if exit_code == 0 else f"FAIL {exit_code}"
            summary = _summarize_shell_command(command_text)
            events.append(_session_event_block(ts_label, f"工具 {status}", summary))

    return events[-entry_count:], len(events)


def _fit_entries_to_budget(entries: List[str], max_chars: int) -> Tuple[List[str], int]:
    selected: List[str] = []
    total_chars = 0
    for entry in reversed(entries):
        entry_len = len(entry) + (2 if selected else 0)
        if selected and total_chars + entry_len > max_chars:
            break
        selected.append(entry)
        total_chars += entry_len
    selected.reverse()
    return selected, len(entries) - len(selected)


def format_recent_logs(user_text: str) -> str:
    normalized = (user_text or "").strip().lower()
    line_count = requested_log_lines(user_text)
    if wants_codex_session_logs(user_text):
        entry_count = requested_session_entries(user_text)
        session_path = _latest_codex_session_path()
        if session_path is None:
            return "未找到 Codex 桌面任务记录：~/.codex/sessions/**/rollout-*.jsonl"
        events, total_events = _format_codex_session_events(session_path, entry_count)
        events = [redact_log_text(entry) for entry in events]
        header = (
            "Codex 桌面任务记录\n"
            f"会话: {_short_session_path(session_path)}\n"
            f"范围: 最近 {min(entry_count, total_events)} 条 / 共 {total_events} 条"
        )
        budget = 3200 - len(header) - 180
        selected_events, budget_omitted = _fit_entries_to_budget(events, max(900, budget))
        earlier_omitted = max(0, total_events - len(events))
        notes = []
        if earlier_omitted:
            notes.append(f"已省略更早 {earlier_omitted} 条")
        if budget_omitted:
            notes.append(f"为避免飞书截断，又省略 {budget_omitted} 条较早记录")
        if notes:
            header += "\n" + "；".join(notes)
        session_text = "\n\n".join(selected_events)
        return (
            f"{header}\n\n"
            f"{session_text or '（任务记录为空）'}"
        )
    if wants_extension_logs(user_text):
        log_path = _latest_codex_desktop_log_path()
        source_label = "Codex 扩展运行日志"
        if log_path is None:
            return "未找到 Codex 扩展运行日志文件：~/Library/Application Support/Code/logs/*/window*/exthost/openai.chatgpt/Codex.log"
    else:
        log_name = "launchd.err.log" if any(word in normalized for word in ("错误", "err", "error", "异常")) else "launchd.out.log"
        log_path = _BASE_DIR / "logs" / log_name
        source_label = "机器人日志"
    if not log_path.exists():
        return f"未找到{source_label}文件：{log_path}"

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as ex:
        return f"读取{source_label}失败：{type(ex).__name__}: {ex}"

    tail_text = "\n".join(lines[-line_count:])
    tail_text = redact_log_text(tail_text)
    max_chars = 3200
    if len(tail_text) > max_chars:
        clipped_lines = []
        total_chars = 0
        for line in reversed(tail_text.splitlines()):
            line_len = len(line) + 1
            if clipped_lines and total_chars + line_len > max_chars:
                break
            clipped_lines.append(line)
            total_chars += line_len
        tail_text = "…\n" + "\n".join(reversed(clipped_lines))

    return (
        f"最近 {min(line_count, len(lines))} 行 {source_label}：{log_path}\n"
        f"{tail_text or '（日志为空）'}"
    )


# ---------------------------------------------------------------------------
# Task trace logging
# ---------------------------------------------------------------------------

_TRACE_LOCK = threading.Lock()
_TRACE_EVENTS_BY_USER: Dict[str, List[Dict[str, object]]] = {}
_TRACE_MAX_EVENTS_PER_USER = 300
_TRACE_MAX_USERS = 50


def _trace_state_file_path() -> Path:
    return _BASE_DIR / ".state" / "codex_task_traces.jsonl"


def trace_append(open_id: str, seq: int, kind: str, title: str, detail: str = "") -> None:
    if not open_id:
        return
    now = time.time()
    event = {
        "ts": now,
        "open_id": open_id,
        "seq": seq,
        "kind": kind,
        "title": redact_log_text(preview_text(title, 80)),
        "detail": redact_log_text(preview_text(detail, 180)),
    }
    with _TRACE_LOCK:
        events = _TRACE_EVENTS_BY_USER.setdefault(open_id, [])
        events.append(event)
        if len(events) > _TRACE_MAX_EVENTS_PER_USER:
            del events[: -_TRACE_MAX_EVENTS_PER_USER]
        if len(_TRACE_EVENTS_BY_USER) > _TRACE_MAX_USERS:
            oldest_key = min(
                (k for k in _TRACE_EVENTS_BY_USER if k != open_id),
                key=lambda k: _TRACE_EVENTS_BY_USER[k][-1]["ts"] if _TRACE_EVENTS_BY_USER[k] else 0,
                default=None,
            )
            if oldest_key:
                del _TRACE_EVENTS_BY_USER[oldest_key]
    try:
        path = _trace_state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
                rotated = path.with_suffix(".jsonl.old")
                path.replace(rotated)
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        lark.logger.debug("failed to append task trace", exc_info=True)


def trace_events_for_user(open_id: str, seq: Optional[int] = None) -> List[Dict[str, object]]:
    with _TRACE_LOCK:
        events = [dict(event) for event in _TRACE_EVENTS_BY_USER.get(open_id, [])]
    if seq is not None:
        events = [event for event in events if event.get("seq") == seq]
    return events


def format_task_trace(open_id: str, user_text: str) -> str:
    from message_queue import message_queue

    entry_count = requested_trace_entries(user_text)
    active_entry, last_entry = message_queue.get_status(open_id)
    entry = active_entry or last_entry
    if not entry:
        return "当前没有正在执行的任务，也没有本次进程内的过程日志。"

    elapsed = int(time.time() - entry.started_at) if entry.started_at else 0
    state_label = "运行中" if active_entry else ("已完成" if entry.ok else "失败/取消")
    header = [
        "任务过程",
        f"状态: {state_label}",
        f"来源: {entry.task.source}",
        f"任务: {preview_text(entry.task.text) or '（空）'}",
        f"阶段: {entry.stage}",
        f"耗时: {elapsed}s",
    ]
    if entry.detail:
        header.append(f"详情: {redact_log_text(entry.detail)}")

    trace_entries = message_queue.get_trace(open_id, last_n=entry_count)
    if not trace_entries:
        return "\n".join(header) + "\n\n（暂无过程事件）"

    all_trace = message_queue.get_trace(open_id, last_n=200)
    selected = all_trace[-entry_count:]
    omitted = max(0, len(all_trace) - len(selected))
    if omitted:
        header.append(f"已省略更早 {omitted} 条")

    lines = []
    for ts_val, seq, source, event, detail in selected:
        ts_str = time.strftime("%H:%M:%S", time.localtime(ts_val))
        if detail:
            lines.append(f"{ts_str} [{source}] {event}\n  {detail}")
        else:
            lines.append(f"{ts_str} [{source}] {event}")

    body = "\n\n".join(lines) if lines else "（暂无过程事件）"
    reply = "\n".join(header) + "\n\n" + body
    if len(reply) <= 3300:
        return reply

    fitted, budget_omitted = _fit_entries_to_budget(lines, 3300 - len("\n".join(header)) - 120)
    if budget_omitted:
        header.append(f"为避免飞书截断，又省略 {budget_omitted} 条较早记录")
    return "\n".join(header) + "\n\n" + ("\n\n".join(fitted) if fitted else "（暂无过程事件）")


# ---------------------------------------------------------------------------
# User status formatting
# ---------------------------------------------------------------------------


def format_user_status(open_id: str) -> str:
    from message_queue import message_queue

    active_entry, last_entry = message_queue.get_status(open_id)
    if active_entry:
        elapsed = int(time.time() - active_entry.started_at)
        lines = [
            "正在执行",
            f"来源：{active_entry.task.source}",
            f"任务：{preview_text(active_entry.task.text)}",
            f"阶段：{active_entry.stage}",
            f"耗时：{elapsed}s",
        ]
        if active_entry.detail:
            lines.append(f"详情：{redact_log_text(active_entry.detail)}")
        return "\n".join(lines)
    if last_entry:
        label = "上次任务已完成" if last_entry.ok else "上次任务失败"
        elapsed = int(last_entry.last_updated_at - last_entry.started_at) if last_entry.started_at else 0
        lines = [
            label,
            f"来源：{last_entry.task.source}",
            f"任务：{preview_text(last_entry.task.text)}",
            f"阶段：{last_entry.stage}",
            f"耗时：{elapsed}s",
        ]
        if last_entry.detail:
            lines.append(f"详情：{redact_log_text(last_entry.detail)}")
        lines.append(f"完成时间：{time.strftime('%H:%M:%S', time.localtime(last_entry.last_updated_at))}")
        return "\n".join(lines)
    return "当前没有正在执行的任务，也没有本次进程内的历史任务记录。"
