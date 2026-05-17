import json
import os
from dataclasses import dataclass
from typing import List, Set

from dotenv import load_dotenv

load_dotenv()


def _ensure_feishu_no_proxy() -> None:
    hosts = {"open.feishu.cn", "msg-frontier.feishu.cn", ".feishu.cn", "ilinkai.weixin.qq.com", ".weixin.qq.com"}
    existing_raw = os.getenv("NO_PROXY") or os.getenv("no_proxy") or ""
    existing = {item.strip() for item in existing_raw.split(",") if item.strip()}
    merged = sorted(existing.union(hosts))
    value = ",".join(merged)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


_ensure_feishu_no_proxy()


@dataclass
class Settings:
    app_id: str
    app_secret: str
    feishu_http_timeout_sec: int
    openai_api_key: str
    openai_model: str
    use_codex_cli: bool
    codex_cmd: str
    codex_workdir: str
    codex_timeout_sec: int
    codex_model: str
    codex_project_root: str
    codex_sandbox: str
    codex_add_dirs: List[str]
    codex_resume_enabled: bool
    codex_retry_fresh_on_timeout: bool
    allowed_user_ids: Set[str]
    dedup_ttl_sec: int
    dedup_max_ids: int
    codex_thread_state_file: str
    memory_enabled: bool
    codex_memory_turns: int
    codex_memory_state_file: str
    codex_status_update_enabled: bool
    codex_status_poll_sec: int
    codex_status_followup_sec: int
    backend: str
    use_claude_cli: bool
    claude_cmd: str
    claude_workdir: str
    claude_timeout_sec: int
    claude_model: str
    claude_permission_mode: str
    claude_add_dirs: List[str]
    claude_resume_enabled: bool
    claude_retry_fresh_on_timeout: bool
    claude_session_state_file: str

    @staticmethod
    def from_env() -> "Settings":
        allowed_raw = os.getenv("ALLOWED_USER_IDS", "").strip()
        allowed = {x.strip() for x in allowed_raw.split(",") if x.strip()}

        use_codex_cli = os.getenv("USE_CODEX_CLI", "true").strip().lower() in ("1", "true", "yes", "on")
        codex_resume_enabled = os.getenv("CODEX_RESUME_ENABLED", "true").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        codex_retry_fresh_on_timeout = os.getenv("CODEX_RETRY_FRESH_ON_TIMEOUT", "true").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        memory_enabled_raw = os.getenv("MEMORY_ENABLED", "").strip() or os.getenv("CODEX_MEMORY_ENABLED", "true").strip()
        memory_enabled = memory_enabled_raw.lower() in ("1", "true", "yes", "on")
        codex_status_update_enabled = os.getenv("CODEX_STATUS_UPDATE_ENABLED", "true").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        timeout_raw = os.getenv("CODEX_TIMEOUT_SEC", "120").strip()
        codex_sandbox_raw = os.getenv("CODEX_SANDBOX", "workspace-write").strip().lower()
        codex_add_dirs_raw = os.getenv("CODEX_ADD_DIRS", "").strip()
        feishu_timeout_raw = os.getenv("FEISHU_HTTP_TIMEOUT_SEC", "20").strip()
        dedup_ttl_raw = os.getenv("DEDUPE_TTL_SEC", "900").strip()
        dedup_max_raw = os.getenv("DEDUPE_MAX_IDS", "2000").strip()
        memory_turns_raw = os.getenv("CODEX_MEMORY_TURNS", "6").strip()
        status_poll_raw = os.getenv("CODEX_STATUS_POLL_SEC", "3").strip()
        status_followup_raw = os.getenv("CODEX_STATUS_FOLLOWUP_SEC", "30").strip()

        try:
            timeout_sec = int(timeout_raw)
        except ValueError:
            timeout_sec = 120
        try:
            feishu_http_timeout_sec = max(5, min(120, int(feishu_timeout_raw)))
        except ValueError:
            feishu_http_timeout_sec = 20

        try:
            dedup_ttl_sec = max(30, int(dedup_ttl_raw))
        except ValueError:
            dedup_ttl_sec = 900

        try:
            dedup_max_ids = max(100, int(dedup_max_raw))
        except ValueError:
            dedup_max_ids = 2000
        try:
            codex_memory_turns = max(1, min(20, int(memory_turns_raw)))
        except ValueError:
            codex_memory_turns = 6
        try:
            codex_status_poll_sec = max(2, min(30, int(status_poll_raw)))
        except ValueError:
            codex_status_poll_sec = 3
        try:
            codex_status_followup_sec = max(10, min(300, int(status_followup_raw)))
        except ValueError:
            codex_status_followup_sec = 30
        codex_sandbox = codex_sandbox_raw if codex_sandbox_raw in ("read-only", "workspace-write", "danger-full-access") else "workspace-write"
        codex_add_dirs = [x.strip() for x in codex_add_dirs_raw.split(",") if x.strip()]

        backend_raw = os.getenv("BACKEND", "claude").strip().lower()
        backend = backend_raw if backend_raw in ("claude", "codex") else "claude"

        use_claude_cli = os.getenv("USE_CLAUDE_CLI", "true").strip().lower() in ("1", "true", "yes", "on")
        claude_resume_enabled = os.getenv("CLAUDE_RESUME_ENABLED", "true").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        claude_retry_fresh_on_timeout = os.getenv("CLAUDE_RETRY_FRESH_ON_TIMEOUT", "true").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        try:
            claude_timeout_sec = int(os.getenv("CLAUDE_TIMEOUT_SEC", str(timeout_sec)).strip())
        except ValueError:
            claude_timeout_sec = timeout_sec
        claude_permission_raw = os.getenv("CLAUDE_PERMISSION_MODE", "acceptEdits").strip()
        valid_permission_modes = {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
        claude_permission_mode = (
            claude_permission_raw if claude_permission_raw in valid_permission_modes else "acceptEdits"
        )
        claude_add_dirs_raw = os.getenv("CLAUDE_ADD_DIRS", "").strip()
        claude_add_dirs = [x.strip() for x in claude_add_dirs_raw.split(",") if x.strip()]

        return Settings(
            app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            feishu_http_timeout_sec=feishu_http_timeout_sec,
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini").strip(),
            use_codex_cli=use_codex_cli,
            codex_cmd=os.getenv("CODEX_CLI_CMD", "codex").strip() or "codex",
            codex_workdir=os.getenv("CODEX_WORKDIR", "/Users/cn/Workspace").strip() or "/Users/cn/Workspace",
            codex_timeout_sec=timeout_sec,
            codex_model=os.getenv("CODEX_MODEL", "").strip(),
            codex_project_root=os.getenv("CODEX_PROJECT_ROOT", "/Users/cn/Workspace").strip() or "/Users/cn/Workspace",
            codex_sandbox=codex_sandbox,
            codex_add_dirs=codex_add_dirs,
            codex_resume_enabled=codex_resume_enabled,
            codex_retry_fresh_on_timeout=codex_retry_fresh_on_timeout,
            allowed_user_ids=allowed,
            dedup_ttl_sec=dedup_ttl_sec,
            dedup_max_ids=dedup_max_ids,
            codex_thread_state_file=(
                os.getenv("CODEX_THREAD_STATE_FILE", ".state/codex_threads.json").strip() or ".state/codex_threads.json"
            ),
            memory_enabled=memory_enabled,
            codex_memory_turns=codex_memory_turns,
            codex_memory_state_file=(
                os.getenv("CODEX_MEMORY_STATE_FILE", ".state/codex_memory.json").strip() or ".state/codex_memory.json"
            ),
            codex_status_update_enabled=codex_status_update_enabled,
            codex_status_poll_sec=codex_status_poll_sec,
            codex_status_followup_sec=codex_status_followup_sec,
            backend=backend,
            use_claude_cli=use_claude_cli,
            claude_cmd=os.getenv("CLAUDE_CLI_CMD", "claude").strip() or "claude",
            claude_workdir=(
                os.getenv("CLAUDE_WORKDIR", "").strip()
                or os.getenv("CODEX_WORKDIR", "/Users/cn/Workspace").strip()
                or "/Users/cn/Workspace"
            ),
            claude_timeout_sec=claude_timeout_sec,
            claude_model=os.getenv("CLAUDE_MODEL", "").strip(),
            claude_permission_mode=claude_permission_mode,
            claude_add_dirs=claude_add_dirs,
            claude_resume_enabled=claude_resume_enabled,
            claude_retry_fresh_on_timeout=claude_retry_fresh_on_timeout,
            claude_session_state_file=(
                os.getenv("CLAUDE_SESSION_STATE_FILE", ".state/claude_sessions.json").strip()
                or ".state/claude_sessions.json"
            ),
        )


@dataclass
class ReplyResult:
    ok: bool
    reply: str
    status: str


SETTINGS = Settings.from_env()

if not SETTINGS.app_id or not SETTINGS.app_secret:
    raise RuntimeError("Missing FEISHU_APP_ID / FEISHU_APP_SECRET in .env")
