"""
ws_bot.py - Main entry point: routing, reply orchestration, Feishu event handler.
"""

import json
import threading
from typing import Callable, Dict, List, Optional, Tuple

import httpx
import lark_oapi as lark

from config import SETTINGS, ReplyResult
from feishu_api import LARK_CLIENT, reply_text, update_text_message
from state import (
    clear_claude_session_id,
    clear_thread_id,
    is_duplicate_recent,
)
from memory import append_user_memory, clear_user_memory, get_user_memory
from text_utils import (
    extract_text,
    is_desktop_codex_status_command,
    is_logs_command,
    is_status_command,
    is_trace_command,
    preview_text,
)
from log_viewer import (
    format_desktop_codex_status,
    format_recent_logs,
    format_task_trace,
    format_user_status,
)
from codex_runner import generate_reply_via_codex
from claude_session import CLAUDE_SESSION, generate_reply_via_claude
from message_queue import message_queue, MessageTask


# ---------------------------------------------------------------------------
# Backend & mode resolution
# ---------------------------------------------------------------------------

_BACKEND_PREFIXES: Dict[str, str] = {
    "/cc": "claude",
    "/claude": "claude",
    "/codex": "codex",
}


def _resolve_backend_and_text(user_text: str) -> Tuple[str, str]:
    stripped = user_text.strip()
    if not stripped:
        return SETTINGS.backend, stripped
    parts = stripped.split(None, 1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    backend = _BACKEND_PREFIXES.get(head.lower())
    if backend:
        return backend, rest.strip()
    return SETTINGS.backend, stripped


def _is_reset_command(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"/reset", "重置会话", "清空记忆"}


_SKILL_TRIGGERS = {
    "commit": "git-essentials",
    "push": "git-essentials",
    "pull": "git-essentials",
    "merge": "git-essentials",
    "rebase": "git-essentials",
    "cherry-pick": "git-essentials",
    "提交": "git-essentials",
    "推送": "git-essentials",
    "同步到github": "git-essentials",
    "gitee": "data-catalog-gitee-push",
    "data-catalog": "data-catalog-gitee-push",
    "安全审计": "security-audit",
    "安全扫描": "security-audit",
    "vulnerability": "security-audit",
    "pr merge": "auto-pr-merger",
    "auto merge": "auto-pr-merger",
}


def _check_skill_override(user_text: str) -> Optional[str]:
    t = user_text.strip().lower()
    for trigger, skill in _SKILL_TRIGGERS.items():
        if trigger in t:
            return skill
    return None


def _auto_route_mode(user_text: str) -> str:
    skill = _check_skill_override(user_text)
    if skill:
        return "single"
    try:
        from multi_agent import route_message
        return route_message(user_text, CLAUDE_SESSION)
    except Exception:
        return "single"


def _is_wx_user(open_id: str) -> bool:
    return "@im.wechat" in open_id or "@im.bot" in open_id


def _send_to_user(open_id: str, text: str) -> None:
    if _is_wx_user(open_id):
        try:
            from wx_channel import wx_send_text, _CONTEXT_TOKENS, _CONTEXT_TOKEN_TS, _CONTEXT_TOKEN_MAX_AGE
            import time as _t
            ctx = ""
            token_ts = _CONTEXT_TOKEN_TS.get(open_id, 0)
            if _t.time() - token_ts <= _CONTEXT_TOKEN_MAX_AGE:
                ctx = _CONTEXT_TOKENS.get(open_id, "")
            ok = wx_send_text(open_id, text, ctx)
            if not ok:
                lark.logger.warning("[send_to_user] wx_send_text failed for: %s", text[:60])
        except Exception as ex:
            lark.logger.warning("[send_to_user] wx exception: %s, text: %s", ex, text[:60])
    else:
        reply_text(open_id, text)


# ---------------------------------------------------------------------------
# Team mode
# ---------------------------------------------------------------------------


def _generate_reply_team_mode(
    user_text: str,
    open_id: str,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> ReplyResult:
    try:
        from multi_agent import handle_team_message
        import time as _t

        _notify_buffer = []

        def notify(msg: str) -> None:
            _notify_buffer.append(msg)
            if progress_callback:
                progress_callback(msg[:40], "")

        result = handle_team_message(user_text, open_id, CLAUDE_SESSION, notify_fn=notify)
        if _notify_buffer:
            summary = "\n".join(_notify_buffer[-15:])
            _send_to_user(open_id, summary)
            _notify_buffer.clear()
        lark.logger.info("[team_mode] reply len=%d preview=%s", len(result), result[:80])
        append_user_memory(open_id, "user", user_text)
        append_user_memory(open_id, "assistant", result[:500])
        return ReplyResult(True, result, "team_ok")
    except Exception as ex:
        lark.logger.error("[team_mode] exception: %s", ex)
        return ReplyResult(False, f"团队模式异常: {ex}", "team_error")


# ---------------------------------------------------------------------------
# Main reply dispatcher
# ---------------------------------------------------------------------------


def _generate_reply(
    user_text: str,
    open_id: str,
    progress_callback: Optional[Callable[[str, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> ReplyResult:
    backend, payload_text = _resolve_backend_and_text(user_text)

    if _is_reset_command(payload_text) or _is_reset_command(user_text):
        clear_thread_id(open_id)
        clear_claude_session_id(open_id)
        clear_user_memory(open_id)
        if progress_callback:
            progress_callback("已清空上下文", "")
        return ReplyResult(True, "已清空当前会话上下文（claude + codex + 本地记忆）。", "reset")

    if not payload_text:
        return ReplyResult(True, "已切换后端但消息为空，请直接发送你的问题。", "empty_after_prefix")

    if backend == "claude":
        from multi_agent import get_workflow
        active_wf = get_workflow(open_id)
        if active_wf and active_wf.get("phase", "").startswith("awaiting"):
            return _generate_reply_team_mode(payload_text, open_id, progress_callback)

        if payload_text.startswith("/team "):
            team_text = payload_text[6:].strip()
            if team_text:
                return _generate_reply_team_mode(team_text, open_id, progress_callback)

        mode = _auto_route_mode(payload_text)
        if mode == "team":
            return _generate_reply_team_mode(payload_text, open_id, progress_callback)

        claude_reply = generate_reply_via_claude(
            payload_text,
            open_id,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
        if claude_reply:
            return claude_reply
    else:
        codex_reply = generate_reply_via_codex(
            payload_text,
            open_id,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
        if codex_reply:
            return codex_reply

    if not SETTINGS.openai_api_key:
        return ReplyResult(True, f"已收到：{payload_text}\n（当前未配置 OPENAI_API_KEY，先回显模式）", "echo")

    if progress_callback:
        progress_callback("OpenAI API 正在处理", SETTINGS.openai_model)

    headers = {
        "Authorization": f"Bearer {SETTINGS.openai_api_key}",
        "Content-Type": "application/json",
    }
    input_items: List[Dict[str, str]] = [
        {
            "role": "system",
            "content": "You are a concise assistant in Feishu chat.",
        }
    ]
    if SETTINGS.memory_enabled:
        for turn in get_user_memory(open_id):
            role = turn.get("role", "")
            text = turn.get("text", "")
            if role in ("user", "assistant") and text:
                input_items.append({"role": role, "content": text})
    input_items.append({"role": "user", "content": payload_text})
    payload = {"model": SETTINGS.openai_model, "input": input_items}

    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)
        if resp.status_code != 200:
            return ReplyResult(False, f"模型调用失败：HTTP {resp.status_code} {resp.text[:200]}", "openai_http_error")
        data = resp.json()
        output = (data.get("output_text") or "").strip() or "收到，你可以继续发问题。"
        append_user_memory(open_id, "user", payload_text)
        append_user_memory(open_id, "assistant", output)
        return ReplyResult(True, output, "openai_ok")
    except Exception as ex:
        return ReplyResult(False, f"模型调用异常：{ex}", "openai_exception")


# ---------------------------------------------------------------------------
# Feishu event handler
# ---------------------------------------------------------------------------


def do_p2_im_message_receive_v1(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    header = data.header
    event_id: Optional[str] = header.event_id if header else None
    event = data.event
    if not event or not event.message or not event.sender:
        return

    if event.message.message_type != "text":
        return

    sender_id = event.sender.sender_id
    open_id: Optional[str] = sender_id.open_id if sender_id else None
    if not open_id:
        return

    message_id = event.message.message_id
    if is_duplicate_recent(event_id, message_id):
        lark.logger.info("ignored duplicated event: event_id=%s message_id=%s", event_id, message_id)
        return

    if SETTINGS.allowed_user_ids and open_id not in SETTINGS.allowed_user_ids:
        lark.logger.info("ignored message from non-allowed user: %s", open_id)
        return

    user_text = extract_text(event.message.content or "")
    if not user_text:
        return

    if is_desktop_codex_status_command(user_text):
        lark.logger.info("desktop codex status requested by %s", open_id)
        _send_to_user(open_id, format_desktop_codex_status(user_text))
        return

    if is_status_command(user_text):
        lark.logger.info("status requested by %s", open_id)
        _send_to_user(open_id, format_user_status(open_id))
        return

    if is_trace_command(user_text):
        lark.logger.info("trace requested by %s", open_id)
        _send_to_user(open_id, format_task_trace(open_id, user_text))
        return

    if is_logs_command(user_text):
        lark.logger.info("logs requested by %s", open_id)
        _send_to_user(open_id, format_recent_logs(user_text))
        return

    if user_text.strip() == "/retry":
        from claude_session import ClaudePersistentSession
        partial = ClaudePersistentSession.load_partial()
        if partial:
            resume_text = f"继续之前中断的任务:\n原始请求: {partial.get('text', '')}\n上次进度: {partial.get('tool_log', [])[-2:]}"
            message_queue.enqueue(MessageTask(
                source="feishu",
                user_id=open_id,
                text=resume_text,
                reply_fn=lambda text: _send_to_user(open_id, text),
                generate_reply_fn=_generate_reply,
            ))
        else:
            _send_to_user(open_id, "没有可恢复的中断任务。")
        return

    if user_text.strip().startswith("/review"):
        from memory import get_unreviewed_episodes
        eps = get_unreviewed_episodes(open_id)
        if eps:
            lines = [f"[{e.get('id','')}] {e.get('outcome','')} — {e.get('user_goal','')[:50]}" for e in eps]
            _send_to_user(open_id, "待审阅的学习记录:\n" + "\n".join(lines))
        else:
            _send_to_user(open_id, "没有待审阅的学习记录。")
        return

    if user_text.strip().startswith("/promote "):
        from memory import promote_episode
        ep_id = user_text.strip()[9:].strip()
        ok = promote_episode(open_id, ep_id)
        _send_to_user(open_id, f"✅ 已提升: {ep_id}" if ok else f"❌ 未找到: {ep_id}")
        return

    if user_text.strip().startswith("/reject "):
        from memory import reject_episode
        ep_id = user_text.strip()[8:].strip()
        ok = reject_episode(open_id, ep_id)
        _send_to_user(open_id, f"✅ 已拒绝: {ep_id}" if ok else f"❌ 未找到: {ep_id}")
        return

    lark.logger.info("received message from %s: %s", open_id, user_text[:120])

    message_queue.enqueue(MessageTask(
        source="feishu",
        user_id=open_id,
        text=user_text,
        reply_fn=lambda text: _send_to_user(open_id, text),
        generate_reply_fn=_generate_reply,
    ))


def do_message_event(data: lark.CustomizedEvent) -> None:
    lark.logger.info("customized event received: %s", lark.JSON.marshal(data, indent=2))


event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
    .register_p1_customized_event("message", do_message_event)
    .build()
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    from state import _THREADS_BY_USER, _MEMORY_BY_USER, _CLAUDE_SESSIONS_BY_USER
    print(
        "[feishu-ws] starting long connection bot "
        f"(allowed_user_ids={len(SETTINGS.allowed_user_ids)}, "
        f"backend={SETTINGS.backend}, "
        f"use_codex_cli={SETTINGS.use_codex_cli}, codex_resume={SETTINGS.codex_resume_enabled}, "
        f"codex_model={SETTINGS.codex_model or 'default'}, "
        f"use_claude_cli={SETTINGS.use_claude_cli}, claude_resume={SETTINGS.claude_resume_enabled}, "
        f"claude_model={SETTINGS.claude_model or 'default'}, claude_perm={SETTINGS.claude_permission_mode}, "
        f"openai_model={SETTINGS.openai_model}, "
        f"feishu_timeout={SETTINGS.feishu_http_timeout_sec}s, "
        f"dedup_ttl={SETTINGS.dedup_ttl_sec}s, "
        f"users_with_thread={len(_THREADS_BY_USER)}, users_with_claude_session={len(_CLAUDE_SESSIONS_BY_USER)}, "
        f"memory_enabled={SETTINGS.memory_enabled}, users_with_memory={len(_MEMORY_BY_USER)}, "
        f"status_updates={SETTINGS.codex_status_update_enabled}, poll={SETTINGS.codex_status_poll_sec}s, "
        f"followup={SETTINGS.codex_status_followup_sec}s)"
    )
    from claude_session import warmup_session
    warmup_session()
    print("[feishu-ws] Claude session warming up...")

    try:
        from wx_channel import start_wx_channel
        wx_thread = start_wx_channel(_generate_reply)
        if wx_thread:
            print("[feishu-ws] WeChat channel started")
    except Exception as ex:
        print(f"[feishu-ws] WeChat channel not started: {ex}")

    try:
        from health_monitor import start_health_monitor
        alert_user = list(SETTINGS.allowed_user_ids)[0] if SETTINGS.allowed_user_ids else None
        if alert_user:
            start_health_monitor(CLAUDE_SESSION, message_queue, lambda text: reply_text(alert_user, text))
            print("[feishu-ws] Health monitor started")
    except Exception as ex:
        print(f"[feishu-ws] Health monitor not started: {ex}")

    cli = lark.ws.Client(
        SETTINGS.app_id,
        SETTINGS.app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )
    cli.start()


if __name__ == "__main__":
    main()
