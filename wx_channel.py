"""
WeChat IM Bot channel for feishu-bot-bridge.
Long-poll based, reuses the same Claude Code persistent session as feishu.

Protocol reference: openclaw-weixin plugin (ilinkai.weixin.qq.com API).
"""

import base64
import hashlib
import json
import os
import struct
import threading
import time
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()


class WxBotConfig:
    def __init__(self) -> None:
        self.enabled = os.getenv("WX_BOT_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
        self.base_url = os.getenv("WX_BOT_BASE_URL", "https://ilinkai.weixin.qq.com").strip()
        self.token = os.getenv("WX_BOT_TOKEN", "").strip()
        self.allowed_user_ids = {
            x.strip() for x in os.getenv("WX_BOT_ALLOWED_USER_IDS", "").strip().split(",") if x.strip()
        }
        self.timeout_ms = int(os.getenv("WX_BOT_TIMEOUT_MS", "15000"))
        self.longpoll_timeout_ms = int(os.getenv("WX_BOT_LONGPOLL_TIMEOUT_MS", "35000"))
        self.app_id = os.getenv("WX_BOT_APP_ID", "bot").strip()
        self.channel_version = os.getenv("WX_BOT_CHANNEL_VERSION", "2.4.3").strip()
        self.dedup_ttl_sec = int(os.getenv("WX_DEDUP_TTL_SEC", "900"))
        self.dedup_max_ids = int(os.getenv("WX_DEDUP_MAX_IDS", "2000"))


WX_CONFIG = WxBotConfig()


def _random_wechat_uin() -> str:
    raw = struct.unpack(">I", os.urandom(4))[0]
    return base64.b64encode(str(raw).encode()).decode()


def _build_headers() -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": WX_CONFIG.app_id,
        "iLink-App-ClientVersion": "131587",
    }
    if WX_CONFIG.token:
        headers["Authorization"] = f"Bearer {WX_CONFIG.token}"
    return headers


def _base_info() -> dict:
    return {"channel_version": WX_CONFIG.channel_version, "bot_agent": "FeishuBotBridge/1.0"}


def wx_notify_start() -> bool:
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{WX_CONFIG.base_url}/ilink/bot/msg/notifystart",
                headers=_build_headers(),
                json={"base_info": _base_info()},
            )
        return resp.status_code == 200
    except Exception as ex:
        print(f"[wx] notifyStart failed: {ex}")
        return False


def wx_notify_stop() -> None:
    try:
        with httpx.Client(timeout=5) as client:
            client.post(
                f"{WX_CONFIG.base_url}/ilink/bot/msg/notifystop",
                headers=_build_headers(),
                json={"base_info": _base_info()},
            )
    except Exception:
        pass


def wx_get_updates(get_updates_buf: str = "") -> dict:
    try:
        timeout_sec = WX_CONFIG.longpoll_timeout_ms / 1000 + 5
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(
                f"{WX_CONFIG.base_url}/ilink/bot/getupdates",
                headers=_build_headers(),
                json={"get_updates_buf": get_updates_buf, "base_info": _base_info()},
            )
        if resp.status_code == 200:
            return resp.json()
    except httpx.ReadTimeout:
        return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}
    except Exception as ex:
        print(f"[wx] getUpdates error: {ex}")
        time.sleep(3)
    return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}


def wx_send_text(to_user_id: str, text: str, context_token: str = "") -> bool:
    msg = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": f"feishu-bot-{int(time.time()*1000)}",
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    try:
        with httpx.Client(timeout=WX_CONFIG.timeout_ms / 1000) as client:
            resp = client.post(
                f"{WX_CONFIG.base_url}/ilink/bot/sendmessage",
                headers=_build_headers(),
                json={"msg": msg, "base_info": _base_info()},
            )
        ok = resp.status_code == 200
        if not ok:
            print(f"[wx] sendMessage http_error: status={resp.status_code} body={resp.text[:200]}")
        return ok
    except Exception as ex:
        print(f"[wx] sendMessage failed: {ex}")
        return False


_CONTEXT_TOKENS: Dict[str, str] = {}

_WX_DEDUP_LOCK = threading.Lock()
_WX_SEEN_MSG_IDS: Dict[str, float] = {}


def _wx_is_duplicate(msg_id: str) -> bool:
    now = time.time()
    with _WX_DEDUP_LOCK:
        ttl = WX_CONFIG.dedup_ttl_sec
        stale_keys = [k for k, ts in _WX_SEEN_MSG_IDS.items() if now - ts > ttl]
        for k in stale_keys:
            _WX_SEEN_MSG_IDS.pop(k, None)
        while len(_WX_SEEN_MSG_IDS) > WX_CONFIG.dedup_max_ids:
            oldest_key = next(iter(_WX_SEEN_MSG_IDS))
            _WX_SEEN_MSG_IDS.pop(oldest_key, None)

        if msg_id in _WX_SEEN_MSG_IDS:
            return True
        _WX_SEEN_MSG_IDS[msg_id] = now
        return False


def _build_msg_dedup_key(msg: dict) -> str:
    msg_id = msg.get("msg_id") or msg.get("server_msg_id") or ""
    if msg_id:
        return msg_id
    from_user = msg.get("from_user_id", "")
    create_time = str(msg.get("create_time", ""))
    content = _extract_text_from_message(msg)
    raw = f"{from_user}:{create_time}:{content}"
    return hashlib.md5(raw.encode()).hexdigest()


def _extract_text_from_message(msg: dict) -> str:
    items = msg.get("item_list") or []
    texts = []
    for item in items:
        if item.get("type") == 1:
            text_item = item.get("text_item") or {}
            t = text_item.get("text", "").strip()
            if t:
                texts.append(t)
    return "\n".join(texts)


def start_wx_channel(generate_reply_fn, reply_text_fn=None) -> Optional[threading.Thread]:
    """
    Start the WeChat channel polling loop.

    generate_reply_fn: callable(user_text, open_id, progress_callback, cancel_event) -> ReplyResult
    reply_text_fn: optional callable(open_id, text) for sending intermediate messages
    """
    if not WX_CONFIG.enabled:
        return None
    if not WX_CONFIG.token:
        print("[wx] WX_BOT_TOKEN not set, skipping WeChat channel")
        return None

    from message_queue import message_queue, MessageTask

    def _wx_reply(user_id: str, text: str) -> None:
        ctx = _CONTEXT_TOKENS.get(user_id, "")
        wx_send_text(user_id, text, ctx)

    def _poll_loop() -> None:
        print(f"[wx] starting WeChat channel (base_url={WX_CONFIG.base_url})")
        if not wx_notify_start():
            print("[wx] notifyStart failed, will retry on first getUpdates")

        get_updates_buf = ""
        while True:
            resp = wx_get_updates(get_updates_buf)
            new_buf = resp.get("get_updates_buf")
            if new_buf:
                get_updates_buf = new_buf

            msgs = resp.get("msgs") or []
            for msg in msgs:
                msg_type = msg.get("message_type", 0)
                if msg_type != 1:
                    continue

                from_user = msg.get("from_user_id", "")
                if not from_user:
                    continue

                if WX_CONFIG.allowed_user_ids and from_user not in WX_CONFIG.allowed_user_ids:
                    continue

                dedup_key = _build_msg_dedup_key(msg)
                if _wx_is_duplicate(dedup_key):
                    print(f"[wx] duplicate message skipped: {dedup_key[:32]} from {from_user}")
                    continue

                ctx_token = msg.get("context_token", "")
                if ctx_token:
                    _CONTEXT_TOKENS[from_user] = ctx_token

                user_text = _extract_text_from_message(msg)
                if not user_text:
                    continue

                print(f"[wx] received from {from_user}: {user_text[:80]}")

                last_tool_count = [0]

                def _make_wx_progress(uid: str, counter: list) -> callable:
                    def _progress(stage: str, detail: str = "") -> None:
                        try:
                            from ws_bot import _CLAUDE_SESSION
                        except ImportError:
                            return
                        if not hasattr(_CLAUDE_SESSION, "_tool_log_lock"):
                            return
                        new_entries: List[str] = []
                        with _CLAUDE_SESSION._tool_log_lock:
                            current_count = len(_CLAUDE_SESSION._tool_log)
                            if current_count > counter[0]:
                                new_entries = _CLAUDE_SESSION._tool_log[counter[0]:]
                                counter[0] = current_count
                        if new_entries:
                            _wx_reply(uid, "\n".join(new_entries))
                    return _progress

                message_queue.enqueue(MessageTask(
                    source="wechat",
                    user_id=from_user,
                    text=user_text,
                    reply_fn=lambda text, uid=from_user: _wx_reply(uid, text),
                    generate_reply_fn=generate_reply_fn,
                    on_progress=_make_wx_progress(from_user, last_tool_count),
                ))

    t = threading.Thread(target=_poll_loop, name="wx-channel-poll", daemon=True)
    t.start()
    return t
