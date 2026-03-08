#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import lark_oapi as lark
except ImportError:
    lark = None
from dotenv import load_dotenv


PROJECT_ROOT = Path("/Users/cn/Workspace/feishu-bot-bridge")
DEFAULT_WORKSPACE = Path("/Users/cn/Workspace")
SESSION_TO_MEMORY_SCRIPT = Path("/Users/cn/.codex/skills/session-memory-workspace/scripts/session-to-memory.js")
DEFAULT_DAILY_REPORT_SCOPES = ["codex_snapshot", "work_snapshot"]


def _parse_report_scopes(raw: str) -> List[str]:
    alias_map = {
        "session": "session_summary",
        "sessions": "session_summary",
        "session_summary": "session_summary",
        "session_messages": "session_summary",
        "messages": "session_summary",
        "codex": "codex_snapshot",
        "codex_snapshot": "codex_snapshot",
        "local_codex": "codex_snapshot",
        "work": "work_snapshot",
        "work_snapshot": "work_snapshot",
        "git": "work_snapshot",
        "current_workdir": "work_snapshot",
    }
    items = [x.strip().lower() for x in raw.split(",") if x.strip()]
    if not items:
        return DEFAULT_DAILY_REPORT_SCOPES.copy()
    parsed: List[str] = []
    for item in items:
        normalized = alias_map.get(item)
        if normalized and normalized not in parsed:
            parsed.append(normalized)
    if not parsed:
        return DEFAULT_DAILY_REPORT_SCOPES.copy()
    return parsed


@dataclass
class Config:
    app_id: str
    app_secret: str
    allowed_user_ids: List[str]
    send_open_id: str
    sessions_dir: Path
    workspace_root: Path
    current_workdir: Path
    date_mode: str
    report_scopes: List[str]

    @staticmethod
    def from_env() -> "Config":
        load_dotenv(PROJECT_ROOT / ".env")
        allowed = [x.strip() for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]
        send_open_id = os.getenv("DAILY_REPORT_SEND_OPEN_ID", "").strip() or (allowed[0] if allowed else "")
        sessions_dir_raw = os.getenv("DAILY_REPORT_SESSIONS_DIR", "~/.codex/sessions").strip()
        workspace_raw = os.getenv("DAILY_REPORT_WORKSPACE_ROOT", str(DEFAULT_WORKSPACE)).strip()
        current_workdir_raw = (
            os.getenv("DAILY_REPORT_CURRENT_WORKDIR", "").strip()
            or str(PROJECT_ROOT)
        )
        scope_raw = os.getenv("DAILY_REPORT_SCOPE", "").strip()
        return Config(
            app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            allowed_user_ids=allowed,
            send_open_id=send_open_id,
            sessions_dir=Path(os.path.expanduser(sessions_dir_raw)),
            workspace_root=Path(workspace_raw),
            current_workdir=Path(os.path.expanduser(current_workdir_raw)),
            date_mode=os.getenv("DAILY_REPORT_DATE_MODE", "today").strip().lower(),
            report_scopes=_parse_report_scopes(scope_raw),
        )


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                p_type = str(part.get("type", "")).strip()
                if p_type in ("text", "input_text", "output_text"):
                    text = str(part.get("text", "")).strip()
                    if text:
                        parts.append(text)
            elif isinstance(part, str) and part.strip():
                parts.append(part.strip())
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        maybe_text = str(content.get("text", "")).strip()
        return maybe_text
    return ""


def _read_jsonl(file_path: Path) -> List[dict]:
    out: List[dict] = []
    try:
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return out


def _to_local_date(ts: str) -> Optional[str]:
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00")
        dt_obj = dt.datetime.fromisoformat(normalized)
        if dt_obj.tzinfo is None:
            return dt_obj.date().isoformat()
        return dt_obj.astimezone().date().isoformat()
    except Exception:
        if len(ts) >= 10:
            return ts[:10]
        return None


def _session_date(rows: List[dict]) -> Optional[str]:
    for row in rows:
        ts = row.get("timestamp")
        if isinstance(ts, str):
            date_str = _to_local_date(ts)
            if date_str:
                return date_str
    return None


def _extract_row_message(row: dict) -> Optional[Tuple[str, str]]:
    # openclaw style
    if row.get("type") == "message":
        message = row.get("message") or {}
        role = message.get("role")
        if role in ("user", "assistant"):
            text = _extract_text(message.get("content"))
            if text:
                return role, text
    # codex style
    if row.get("type") == "response_item":
        payload = row.get("payload") or {}
        if payload.get("type") == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                text = _extract_text(payload.get("content"))
                if text:
                    return role, text
    return None


def _candidate_session_files(sessions_dir: Path, report_date: str) -> List[Path]:
    yyyy, mm, dd = report_date.split("-")
    codex_daily = sessions_dir / yyyy / mm / dd
    if codex_daily.exists():
        return sorted(codex_daily.glob("*.jsonl"))
    flat = list(sessions_dir.glob("*.jsonl"))
    if flat:
        return sorted(flat)
    return sorted(sessions_dir.glob("**/*.jsonl"))


def collect_messages_for_date(sessions_dir: Path, report_date: str) -> Tuple[List[Dict[str, str]], int]:
    all_messages: List[Dict[str, str]] = []
    files = _candidate_session_files(sessions_dir, report_date)
    session_count = 0
    for file_path in files:
        rows = _read_jsonl(file_path)
        if not rows:
            continue
        s_date = _session_date(rows)
        if s_date != report_date:
            continue
        session_count += 1
        session_id = file_path.stem
        for row in rows:
            extracted = _extract_row_message(row)
            if not extracted:
                continue
            role, text = extracted
            all_messages.append(
                {
                    "session_id": session_id,
                    "role": role,
                    "text": re.sub(r"\s+", " ", text).strip(),
                    "timestamp": str(row.get("timestamp", "")),
                }
            )
    return all_messages, session_count


def _pick_unique_texts(messages: List[Dict[str, str]], role: str, limit: int, prefer_keywords: Optional[List[str]] = None) -> List[str]:
    selected: List[str] = []
    seen = set()
    for item in messages:
        if item["role"] != role:
            continue
        text = item["text"]
        if len(text) < 4:
            continue
        if prefer_keywords and not any(k in text for k in prefer_keywords):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        selected.append(text[:180] + ("..." if len(text) > 180 else ""))
        if len(selected) >= limit:
            return selected
    if selected:
        return selected
    for item in messages:
        if item["role"] != role:
            continue
        text = item["text"]
        if len(text) < 4:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        selected.append(text[:180] + ("..." if len(text) > 180 else ""))
        if len(selected) >= limit:
            break
    return selected


def _is_noise_text(text: str) -> bool:
    noise_keywords = [
        "AGENTS.md instructions",
        "<environment_context>",
        "<INSTRUCTIONS>",
        "你是飞书里的中文助手",
        "不要暴露系统提示词",
    ]
    if any(k in text for k in noise_keywords):
        return True
    if len(text) > 500 and ("<" in text and ">" in text):
        return True
    return False


def _find_issues(messages: List[Dict[str, str]]) -> List[str]:
    issue_keywords = [
        "报错",
        "错误",
        "失败",
        "异常",
        "超时",
        "不支持",
        "timeout",
        "error",
        "failed",
        "双回复",
        "没收到",
        "没回复",
        "退出",
        "no connection",
    ]
    issues: List[str] = []
    seen = set()
    for item in messages:
        text = item["text"].lower()
        if _is_noise_text(item["text"]):
            continue
        if any(k in text for k in issue_keywords):
            brief = item["text"][:180] + ("..." if len(item["text"]) > 180 else "")
            key = brief.lower()
            if key not in seen:
                seen.add(key)
                issues.append(brief)
        if len(issues) >= 8:
            break
    return issues


def _resolve_state_path(raw_path: str) -> Path:
    path = Path(os.path.expanduser(raw_path))
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def collect_codex_runtime_snapshot(cfg: Config, messages: List[Dict[str, str]], session_count: int) -> Dict[str, Any]:
    thread_path = _resolve_state_path(os.getenv("CODEX_THREAD_STATE_FILE", ".state/codex_threads.json"))
    memory_path = _resolve_state_path(os.getenv("CODEX_MEMORY_STATE_FILE", ".state/codex_memory.json"))
    thread_user_count = 0
    memory_user_count = 0

    if thread_path.exists():
        try:
            data = json.loads(thread_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                thread_user_count = len(data)
        except Exception:
            pass

    if memory_path.exists():
        try:
            data = json.loads(memory_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                memory_user_count = len(data)
        except Exception:
            pass

    clean_messages = [m for m in messages if not _is_noise_text(m["text"])]
    top_user_intents = _pick_unique_texts(clean_messages, role="user", limit=3)

    return {
        "sessions_dir": str(cfg.sessions_dir),
        "session_count": session_count,
        "message_count": len(messages),
        "thread_state_path": str(thread_path),
        "thread_user_count": thread_user_count,
        "memory_state_path": str(memory_path),
        "memory_user_count": memory_user_count,
        "top_user_intents": top_user_intents,
    }


def _run_git(workdir: Path, args: List[str], timeout_sec: int = 8) -> Tuple[int, str, str]:
    result = subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def collect_work_snapshot(report_date: str, workdir: Path) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "workdir": str(workdir),
        "is_git_repo": False,
        "branch": "",
        "status_items": [],
        "today_commits": [],
        "errors": [],
    }
    if not workdir.exists():
        snapshot["errors"].append("工作目录不存在。")
        return snapshot

    code, out, err = _run_git(workdir, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out != "true":
        snapshot["errors"].append(err[:180] if err else "目录不是 Git 仓库。")
        return snapshot

    snapshot["is_git_repo"] = True
    _, branch, _ = _run_git(workdir, ["branch", "--show-current"])
    snapshot["branch"] = branch or "(detached)"

    _, status_out, _ = _run_git(workdir, ["status", "--porcelain"])
    snapshot["status_items"] = [line.rstrip() for line in status_out.splitlines() if line.strip()]

    since_text = f"{report_date} 00:00:00"
    until_text = f"{report_date} 23:59:59"
    _, log_out, _ = _run_git(
        workdir,
        ["log", "--since", since_text, "--until", until_text, "--pretty=format:%h %s", "--max-count", "8"],
    )
    snapshot["today_commits"] = [line for line in log_out.splitlines() if line.strip()]
    return snapshot


def build_report_markdown(
    report_date: str,
    messages: List[Dict[str, str]],
    session_count: int,
    codex_snapshot: Dict[str, Any],
    work_snapshot: Dict[str, Any],
    report_scopes: List[str],
) -> str:
    enabled_scopes = set(report_scopes)
    clean_messages = [m for m in messages if not _is_noise_text(m["text"])]
    user_tasks = _pick_unique_texts(clean_messages, role="user", limit=8)
    outcomes = _pick_unique_texts(
        clean_messages,
        role="assistant",
        limit=8,
        prefer_keywords=["完成", "修复", "新增", "重启", "启动", "发送", "配置", "已", "成功"],
    )
    issues = _find_issues(clean_messages)
    total_msgs = len(messages)
    user_count = sum(1 for m in messages if m["role"] == "user")
    assistant_count = sum(1 for m in messages if m["role"] == "assistant")

    reflection = [
        "对模型、账号、外部 API 的可用性先做最小连通测试，再切到自动化链路。",
        "事件驱动消息默认启用幂等去重与结构化日志，避免重复投递带来的双动作。",
        "进程统一由 launchd 托管，避免手工多实例导致状态漂移。",
    ]
    if not issues:
        reflection = [
            "今天整体链路稳定，继续保持变更后即时验证和日志核对。",
            "后续优先优化响应速度与日志可读性，缩短排障时间。",
        ]

    lines: List[str] = []
    lines.append(f"# 日报（{report_date}）")
    lines.append("")
    lines.append("## 报告范围")
    lines.append(f"- 已启用：{', '.join(report_scopes)}")
    lines.append("")

    if "session_summary" in enabled_scopes:
        lines.append("## 今日概览")
        lines.append(f"- 会话数：{session_count}")
        lines.append(f"- 消息数：{total_msgs}（用户 {user_count} / 助手 {assistant_count}）")
        lines.append("")
        lines.append("## 今日工作内容")
        if user_tasks:
            for t in user_tasks:
                lines.append(f"- {t}")
        else:
            lines.append("- 今日未检索到有效任务消息。")
        lines.append("")
        lines.append("## 执行结果")
        if outcomes:
            for o in outcomes:
                lines.append(f"- {o}")
        else:
            lines.append("- 今日未检索到明确的执行结果描述。")
        lines.append("")

    if "codex_snapshot" in enabled_scopes:
        lines.append("## 本机 Codex 快照")
        lines.append(f"- 会话来源目录：`{codex_snapshot.get('sessions_dir', '')}`")
        lines.append(
            f"- 当日会话/消息：{codex_snapshot.get('session_count', 0)} / {codex_snapshot.get('message_count', 0)}"
        )
        lines.append(
            f"- 线程映射用户数：{codex_snapshot.get('thread_user_count', 0)}（`{codex_snapshot.get('thread_state_path', '')}`）"
        )
        lines.append(
            f"- 本地短记忆用户数：{codex_snapshot.get('memory_user_count', 0)}（`{codex_snapshot.get('memory_state_path', '')}`）"
        )
        top_intents = codex_snapshot.get("top_user_intents", []) or []
        if top_intents:
            lines.append("- 本机 Codex 高频任务：")
            for item in top_intents:
                lines.append(f"  - {item}")
        else:
            lines.append("- 本机 Codex 高频任务：今日无可提取项。")
        lines.append("")

    if "work_snapshot" in enabled_scopes:
        lines.append("## 当前窗口工作快照")
        lines.append(f"- 工作目录：`{work_snapshot.get('workdir', '')}`")
        if work_snapshot.get("is_git_repo"):
            lines.append(f"- Git 分支：{work_snapshot.get('branch', '(unknown)')}")
            status_items = work_snapshot.get("status_items", []) or []
            lines.append(f"- 未提交改动数：{len(status_items)}")
            if status_items:
                lines.append("- 改动文件（前 10 条）：")
                for row in status_items[:10]:
                    lines.append(f"  - `{row}`")
            today_commits = work_snapshot.get("today_commits", []) or []
            if today_commits:
                lines.append("- 今日提交（前 8 条）：")
                for commit in today_commits[:8]:
                    lines.append(f"  - {commit}")
            else:
                lines.append("- 今日提交：暂无。")
        else:
            errors = work_snapshot.get("errors", []) or []
            for err in errors:
                lines.append(f"- {err}")
        lines.append("")

    if "session_summary" in enabled_scopes:
        lines.append("## 问题与异常")
        if issues:
            for i in issues:
                lines.append(f"- {i}")
        else:
            lines.append("- 未发现明显错误关键词。")
        lines.append("")
        lines.append("## 反思与改进")
        for r in reflection:
            lines.append(f"- {r}")
        lines.append("")
        lines.append("## 明日行动")
        lines.append("- 按优先级执行未闭环事项，并在完成后更新日志与记忆。")
        lines.append("- 对关键链路执行一次端到端回归（接收 -> 处理 -> 回发 -> 日志）。")
        lines.append("")

    return "\n".join(lines)


def _upsert_h2_section(file_path: Path, date_str: str, heading: str, section_body: str) -> None:
    section = f"{heading}\n{section_body.strip()}\n"
    if not file_path.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(f"# {date_str}\n\n{section}", encoding="utf-8")
        return
    content = file_path.read_text(encoding="utf-8")
    marker = f"\n{heading}\n"
    if content.startswith(f"{heading}\n"):
        start = 0
    else:
        start = content.find(marker)
    if start == -1:
        updated = content.rstrip() + "\n\n" + section
        file_path.write_text(updated, encoding="utf-8")
        return
    if start > 0:
        start += 1
    next_h2 = content.find("\n## ", start + len(heading) + 1)
    if next_h2 == -1:
        updated = content[:start].rstrip() + "\n\n" + section
    else:
        updated = content[:start].rstrip() + "\n\n" + section + content[next_h2:]
    file_path.write_text(updated, encoding="utf-8")


def write_outputs(report_date: str, report_markdown: str, workspace_root: Path) -> Tuple[Path, Path]:
    report_file = PROJECT_ROOT / "reports" / f"daily-{report_date}.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(report_markdown, encoding="utf-8")

    year = report_date[:4]
    memory_daily = workspace_root / "memory" / "diary" / year / "daily" / f"{report_date}.md"
    memory_section = "\n".join(
        [
            "### Auto Daily Reflection",
            "",
            "以下内容由日报任务自动生成，用于复盘和长期记忆沉淀。",
            "",
            report_markdown,
        ]
    )
    _upsert_h2_section(memory_daily, report_date, "## Session Summary", memory_section)
    return report_file, memory_daily


def sync_session_memory(report_date: str, workspace_root: Path) -> str:
    if not SESSION_TO_MEMORY_SCRIPT.exists():
        return "session-memory script not found, skipped."
    # session-memory script currently expects flat *.jsonl and openclaw schema.
    # Skip auto-sync when the sessions dir is codex nested format to avoid noisy failures.
    sessions_root = Path(os.path.expanduser(os.getenv("DAILY_REPORT_SESSIONS_DIR", "~/.codex/sessions")))
    if (sessions_root / "2026").exists() or (sessions_root / "2025").exists():
        return "session-memory sync skipped for codex nested sessions format."
    try:
        result = subprocess.run(
            [
                "node",
                str(SESSION_TO_MEMORY_SCRIPT),
                "--date",
                report_date,
                "--workspace",
                str(workspace_root),
                "--append",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        text = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0:
            return f"session-memory synced: {text[:240]}"
        return f"session-memory skipped/fail: {text[:240]}"
    except Exception as ex:
        return f"session-memory exception: {ex}"


def _split_text(text: str, max_len: int = 2600) -> List[str]:
    chunks: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len and current:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def send_to_feishu(cfg: Config, title: str, body_markdown: str, dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] skip Feishu sending.")
        return
    if lark is None:
        raise RuntimeError("Missing dependency: lark_oapi. Run `pip install -r requirements.txt`.")
    if not cfg.app_id or not cfg.app_secret:
        raise RuntimeError("Missing FEISHU_APP_ID/FEISHU_APP_SECRET.")
    if not cfg.send_open_id:
        raise RuntimeError("Missing DAILY_REPORT_SEND_OPEN_ID or ALLOWED_USER_IDS.")

    client = (
        lark.Client.builder()
        .app_id(cfg.app_id)
        .app_secret(cfg.app_secret)
        .log_level(lark.LogLevel.INFO)
        .build()
    )
    chunks = _split_text(body_markdown, max_len=2600)
    for idx, chunk in enumerate(chunks, start=1):
        prefix = f"{title} ({idx}/{len(chunks)})\n" if len(chunks) > 1 else f"{title}\n"
        content = json.dumps({"text": prefix + chunk}, ensure_ascii=False)
        req = (
            lark.im.v1.CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                lark.im.v1.CreateMessageRequestBody.builder()
                .receive_id(cfg.send_open_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        resp = client.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(f"Feishu send failed code={resp.code} msg={resp.msg}")
    print(f"Feishu sent {len(chunks)} message(s) to {cfg.send_open_id}.")


def resolve_report_date(cfg: Config, forced_date: Optional[str]) -> str:
    if forced_date:
        return forced_date
    today = dt.datetime.now().date()
    if cfg.date_mode == "yesterday":
        return (today - dt.timedelta(days=1)).isoformat()
    return today.isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate daily report, write memory, and send to Feishu.")
    parser.add_argument("--date", help="Report date, format YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Generate files only, do not send Feishu.")
    args = parser.parse_args()

    cfg = Config.from_env()
    report_date = resolve_report_date(cfg, args.date)
    enabled_scopes = set(cfg.report_scopes)
    needs_messages = bool(enabled_scopes & {"session_summary", "codex_snapshot"})

    if needs_messages:
        messages, session_count = collect_messages_for_date(cfg.sessions_dir, report_date)
    else:
        messages, session_count = [], 0

    codex_snapshot = (
        collect_codex_runtime_snapshot(cfg, messages, session_count)
        if "codex_snapshot" in enabled_scopes
        else {}
    )
    work_snapshot = (
        collect_work_snapshot(report_date, cfg.current_workdir)
        if "work_snapshot" in enabled_scopes
        else {}
    )
    report_md = build_report_markdown(
        report_date,
        messages,
        session_count,
        codex_snapshot,
        work_snapshot,
        cfg.report_scopes,
    )

    report_file, memory_file = write_outputs(report_date, report_md, cfg.workspace_root)
    if "session_summary" in enabled_scopes:
        session_memory_result = sync_session_memory(report_date, cfg.workspace_root)
    else:
        session_memory_result = "session-memory sync skipped by DAILY_REPORT_SCOPE."

    send_to_feishu(cfg, f"【自动日报】{report_date}", report_md, dry_run=args.dry_run)

    print(f"report_file={report_file}")
    print(f"memory_file={memory_file}")
    print(session_memory_result)


if __name__ == "__main__":
    main()
