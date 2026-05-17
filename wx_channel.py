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

_WX_HTTP: Optional[httpx.Client] = None


def _get_wx_http() -> httpx.Client:
    global _WX_HTTP
    if _WX_HTTP is None or _WX_HTTP.is_closed:
        _WX_HTTP = httpx.Client()
    return _WX_HTTP


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
    return {"channel_version": WX_CONFIG.channel_version, "bot_agent": "OpenClaw"}


_TYPING_TICKETS: Dict[str, str] = {}


def wx_get_config(ilink_user_id: str, context_token: str = "") -> Optional[str]:
    """Get typing_ticket for a user via getConfig API."""
    try:
        resp = _get_wx_http().post(
            f"{WX_CONFIG.base_url}/ilink/bot/getconfig",
            headers=_build_headers(),
            json={"ilink_user_id": ilink_user_id, "context_token": context_token, "base_info": _base_info()},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            ticket = data.get("typing_ticket", "")
            if ticket:
                _TYPING_TICKETS[ilink_user_id] = ticket
            return ticket
    except Exception:
        pass
    return None


def wx_send_typing(ilink_user_id: str, status: int = 1) -> bool:
    """Send typing indicator. status: 1=typing, 2=cancel."""
    ticket = _TYPING_TICKETS.get(ilink_user_id, "")
    if not ticket:
        return False
    try:
        resp = _get_wx_http().post(
            f"{WX_CONFIG.base_url}/ilink/bot/sendtyping",
            headers=_build_headers(),
            json={"ilink_user_id": ilink_user_id, "typing_ticket": ticket, "status": status, "base_info": _base_info()},
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


class WxTypingKeepalive:
    """Sends typing indicator every 5s while alive. Call stop() when done."""

    def __init__(self, user_id: str, context_token: str = ""):
        self._user_id = user_id
        self._stop = threading.Event()
        print(f"[typing] init for {user_id[:20]}")
        ticket = wx_get_config(user_id, context_token)
        print(f"[typing] ticket: {'OK' if ticket else 'NONE'}")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[typing] thread started")

    def _loop(self):
        while not self._stop.wait(5):
            ok = wx_send_typing(self._user_id, 1)
            print(f"[typing] keepalive to {self._user_id[:20]}: {'OK' if ok else 'FAIL'}")

    def stop(self):
        self._stop.set()
        wx_send_typing(self._user_id, 2)


def wx_notify_start() -> bool:
    try:
        resp = _get_wx_http().post(
            f"{WX_CONFIG.base_url}/ilink/bot/msg/notifystart",
            headers=_build_headers(),
            json={"base_info": _base_info()},
            timeout=10,
        )
        print(f"[wx] notifyStart: status={resp.status_code} body={resp.text[:200]}")
        return resp.status_code == 200
    except Exception as ex:
        print(f"[wx] notifyStart failed: {ex}")
        return False


def wx_notify_stop() -> None:
    try:
        _get_wx_http().post(
            f"{WX_CONFIG.base_url}/ilink/bot/msg/notifystop",
            headers=_build_headers(),
            json={"base_info": _base_info()},
            timeout=5,
        )
    except Exception:
        pass


def wx_get_updates(get_updates_buf: str = "") -> dict:
    try:
        timeout_sec = WX_CONFIG.longpoll_timeout_ms / 1000 + 5
        resp = _get_wx_http().post(
            f"{WX_CONFIG.base_url}/ilink/bot/getupdates",
            headers=_build_headers(),
            json={"get_updates_buf": get_updates_buf, "base_info": _base_info()},
            timeout=timeout_sec,
        )
        if resp.status_code == 200:
            data = resp.json()
            ret = data.get("ret", 0)
            if ret != 0:
                print(f"[wx] getUpdates api_error: ret={ret} errcode={data.get('errcode')} errmsg={data.get('errmsg','')[:100]}")
            return data
    except httpx.ReadTimeout:
        return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}
    except Exception as ex:
        print(f"[wx] getUpdates error: {ex}")
        time.sleep(3)
    return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}


def _wx_send_once(to_user_id: str, text: str, context_token: str = "") -> int:
    """Send once, return ret code (0=success, negative=error). Uses fresh connection."""
    print(f"[wx] _send_once: len={len(text)}, ctx={'yes' if context_token else 'no'}")
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
    with httpx.Client(timeout=WX_CONFIG.timeout_ms / 1000) as fresh_client:
        resp = fresh_client.post(
            f"{WX_CONFIG.base_url}/ilink/bot/sendmessage",
            headers=_build_headers(),
            json={"msg": msg, "base_info": _base_info()},
        )
    if resp.status_code != 200:
        return -999
    try:
        return resp.json().get("ret", 0)
    except Exception:
        return 0


_wx_send_failures = [0]
_wx_send_count = [0]
_wx_send_window_start = [0.0]
_wx_cooldown_until = [0.0]
_WX_MAX_SENDS_PER_WINDOW = 4
_WX_WINDOW_SEC = 60.0
_WX_COOLDOWN_SEC = 60.0


def wx_send_text(to_user_id: str, text: str, context_token: str = "", priority: bool = False) -> bool:
    now = time.time()
    if now < _wx_cooldown_until[0]:
        if priority:
            remaining = _wx_cooldown_until[0] - now
            print(f"[wx] priority msg waiting {remaining:.0f}s cooldown")
            time.sleep(remaining + 1)
        else:
            print(f"[wx] in cooldown, dropping len={len(text)}")
            return False
    if now - _wx_send_window_start[0] > _WX_WINDOW_SEC:
        _wx_send_count[0] = 0
        _wx_send_window_start[0] = now
    if not priority and _wx_send_count[0] >= _WX_MAX_SENDS_PER_WINDOW:
        print(f"[wx] rate limited, dropping len={len(text)}")
        return False
    _wx_send_count[0] += 1
    try:
        ret = _wx_send_once(to_user_id, text, context_token)
        if ret == 0:
            _wx_send_failures[0] = 0
            return True
        print(f"[wx] sendMessage ret={ret}, len={len(text)}")
        if ret == -2:
            _wx_cooldown_until[0] = time.time() + _WX_COOLDOWN_SEC
            print(f"[wx] ret=-2, cooldown {_WX_COOLDOWN_SEC}s")
        _wx_send_failures[0] += 1
        return False
    except Exception as ex:
        print(f"[wx] sendMessage failed: {ex}")
        return False


_CONTEXT_TOKENS: Dict[str, str] = {}
_CONTEXT_TOKEN_TS: Dict[str, float] = {}
_CONTEXT_TOKEN_MAX_AGE = 55.0
_CONTEXT_TOKENS_MAX = 200

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

    def _wx_reply(user_id: str, text: str, priority: bool = False) -> None:
        ctx = ""
        token_ts = _CONTEXT_TOKEN_TS.get(user_id, 0)
        if time.time() - token_ts <= _CONTEXT_TOKEN_MAX_AGE:
            ctx = _CONTEXT_TOKENS.get(user_id, "")
        else:
            print(f"[wx] context_token expired ({time.time() - token_ts:.0f}s old), sending without")
        wx_send_text(user_id, text, ctx, priority=priority)

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
                    _CONTEXT_TOKEN_TS[from_user] = time.time()
                    print(f"[wx] context_token refreshed for {from_user[:20]}")

                user_text = _extract_text_from_message(msg)
                if not user_text:
                    continue

                print(f"[wx] received from {from_user}: {user_text[:80]}")

                last_tool_count = [0]

                def _make_wx_progress(uid: str, counter: list) -> callable:
                    def _progress(stage: str, detail: str = "") -> None:
                        try:
                            from claude_session import CLAUDE_SESSION as _CLAUDE_SESSION
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

                ctx_token = _CONTEXT_TOKENS.get(from_user, "")
                typing = WxTypingKeepalive(from_user, ctx_token)

                def _wx_reply_with_typing_stop(text, uid=from_user, _typing=typing):
                    _typing.stop()
                    _wx_reply(uid, text, priority=True)

                message_queue.enqueue(MessageTask(
                    source="wechat",
                    user_id=from_user,
                    text=user_text,
                    reply_fn=_wx_reply_with_typing_stop,
                    generate_reply_fn=generate_reply_fn,
                    on_progress=_make_wx_progress(from_user, last_tool_count),
                ))

    t = threading.Thread(target=_poll_loop, name="wx-channel-poll", daemon=True)
    t.start()
    return t
