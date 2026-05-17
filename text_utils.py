import json
import re
from typing import Optional


def preview_text(text: str, limit: int = 80) -> str:
    normalized = " ".join((text or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def is_status_command(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized in {"/status", "status", "状态", "进度", "任务进度", "当前任务"}


def is_desktop_codex_status_command(text: str) -> bool:
    normalized = (text or "").strip().lower()
    compact = normalized.replace(" ", "")
    exact = {
        "桌面状态",
        "桌面进度",
        "桌面任务",
        "桌面任务进度",
        "桌面codex状态",
        "桌面codex进度",
        "codex桌面状态",
        "codex桌面进度",
    }
    if normalized in exact or compact in exact:
        return True
    has_desktop = "桌面" in compact or "desktop" in compact
    has_codex = "codex" in compact
    wants_status = any(word in compact for word in ("进度", "进展", "状态", "当前任务", "任务", "执行", "在干嘛", "做什么"))
    return has_desktop and has_codex and wants_status


def is_trace_command(text: str) -> bool:
    normalized = (text or "").strip().lower()
    compact = normalized.replace(" ", "")
    exact = {
        "/trace",
        "trace",
        "过程日志",
        "执行日志",
        "思考日志",
        "进展日志",
        "进度日志",
        "当前进展",
        "当前进度",
        "任务过程",
        "执行过程",
    }
    if normalized in exact or compact in exact:
        return True
    return ("过程" in compact or "进展" in compact or "进度" in compact or "思考" in compact or "trace" in compact) and (
        "日志" in compact or "记录" in compact or "log" in compact
    )


def is_logs_command(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if normalized in {"/logs", "/log", "logs", "log", "日志", "最新日志", "查看日志", "看日志", "运行日志", "错误日志", "桌面日志", "codex日志", "codex log"}:
        return True
    if wants_codex_session_logs(text) or wants_extension_logs(text):
        return True
    if "日志" not in normalized and "log" not in normalized:
        return False
    intent_words = ("最新", "查看", "看", "发", "给我", "运行", "错误", "err", "error", "tail")
    return any(word in normalized for word in intent_words)


def compact_log_request(text: str) -> str:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized.replace(" ", "")


def wants_extension_logs(text: str) -> bool:
    compact = compact_log_request(text)
    extension_markers = (
        "扩展日志",
        "插件日志",
        "vscode日志",
        "code日志",
        "rawlog",
        "extensionlog",
        "pluginlog",
    )
    if "桌面日志" in compact:
        return False
    return any(marker in compact for marker in extension_markers)


def wants_codex_session_logs(text: str) -> bool:
    compact = compact_log_request(text)
    session_markers = (
        "桌面日志",
        "桌面端日志",
        "codex日志",
        "codexlog",
        "任务日志",
        "对话日志",
        "会话日志",
        "sessionlog",
        "transcript",
        "desktoplog",
    )
    if any(marker in compact for marker in session_markers):
        return True
    return ("桌面" in compact or "codex" in compact or "会话" in compact or "任务" in compact) and (
        "日志" in compact or "log" in compact
    )


def requested_log_lines(text: str) -> int:
    match = re.search(r"(\d{1,3})\s*(?:行|条|lines?|entries?)", text or "", flags=re.IGNORECASE)
    if not match:
        return 40
    try:
        return max(10, min(120, int(match.group(1))))
    except ValueError:
        return 40


def requested_session_entries(text: str) -> int:
    match = re.search(r"(\d{1,3})\s*(?:行|条|lines?|entries?)", text or "", flags=re.IGNORECASE)
    if not match:
        return 12
    try:
        return max(5, min(40, int(match.group(1))))
    except ValueError:
        return 12


def requested_trace_entries(text: str) -> int:
    match = re.search(r"(\d{1,3})\s*(?:行|条|lines?|entries?)", text or "", flags=re.IGNORECASE)
    if not match:
        return 16
    try:
        return max(5, min(60, int(match.group(1))))
    except ValueError:
        return 16


def redact_log_text(text: str) -> str:
    redacted = text
    redacted = re.sub(r'("app_secret"\s*:\s*")[^"]+(")', r"\1<redacted>\2", redacted)
    redacted = re.sub(r"(Authorization\"\s*:\s*\"Bearer\s+)[^\"]+(\")", r"\1<redacted>\2", redacted)
    redacted = re.sub(r"(?i)(\bAuthorization:\s*Bearer\s+)\S+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)((?:api[_-]?key|openai_api_key|x-api-key|cookie|set-cookie)\s*[:=]\s*)[^\s,;\"']+", r"\1<redacted>", redacted)
    redacted = re.sub(r"([?&]access_key=)[^&\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"([?&]ticket=)[^&\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(\baccess_key=)[^&\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"(?i)(\bticket=)[^&\s]+", r"\1<redacted>", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{20,}\b", "sk-<redacted>", redacted)
    redacted = re.sub(r"\bt-[A-Za-z0-9_-]{20,}\b", "t-<redacted>", redacted)
    redacted = re.sub(r"\bBearer\s+[A-Za-z0-9._-]{20,}\b", "Bearer <redacted>", redacted)
    redacted = re.sub(r"\b(cf_clearance|__cf_bm)=[^;\s]+", r"\1=<redacted>", redacted)
    redacted = re.sub(r"\b(?:ou|on|oc|om)_[A-Za-z0-9_-]{12,}\b", lambda m: m.group(0).split("_", 1)[0] + "_<redacted>", redacted)
    return redacted


def extract_text(content_raw: str) -> str:
    if not content_raw:
        return ""
    try:
        obj = json.loads(content_raw)
        if isinstance(obj, dict):
            return str(obj.get("text", "")).strip()
    except Exception:
        pass
    return content_raw.strip()
