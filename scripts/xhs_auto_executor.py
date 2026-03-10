#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False


PROJECT_ROOT = Path("/Users/cn/Workspace/feishu-bot-bridge")


@dataclass
class ExecutorConfig:
    mode: str
    require_approval: bool
    publish_hook_cmd: str
    comment_hook_cmd: str
    publish_driver: str
    post_to_xhs_python_bin: str
    post_to_xhs_script: Path
    post_to_xhs_headless: bool
    post_to_xhs_auto_publish: bool
    post_to_xhs_mode: str
    post_to_xhs_account: str
    session_check_cmd: str
    hook_timeout_sec: int
    comments_per_topic: int
    execution_dir: Path
    account_service: str
    account_key: str
    auth_mode: str
    session_max_age_hours: int
    session_check_required: bool
    relogin_hint: str

    @staticmethod
    def from_env(mode_override: str = "", require_approval_override: bool = False) -> "ExecutorConfig":
        load_dotenv(PROJECT_ROOT / ".env")
        mode = mode_override or os.getenv("XHS_EXECUTOR_MODE", "queue_only").strip() or "queue_only"
        hook_timeout_raw = os.getenv("XHS_EXECUTOR_HOOK_TIMEOUT_SEC", "").strip()
        comments_raw = os.getenv("XHS_COMMENTS_PER_TOPIC", "2").strip()
        session_age_raw = os.getenv("XHS_SESSION_MAX_AGE_HOURS", "72").strip()
        if not hook_timeout_raw:
            hook_timeout_sec = 0
        else:
            try:
                parsed_timeout = int(hook_timeout_raw)
            except ValueError:
                parsed_timeout = 0
            hook_timeout_sec = 0 if parsed_timeout <= 0 else max(5, min(21600, parsed_timeout))
        try:
            comments_per_topic = max(1, min(6, int(comments_raw)))
        except ValueError:
            comments_per_topic = 2
        try:
            session_max_age_hours = max(1, min(720, int(session_age_raw)))
        except ValueError:
            session_max_age_hours = 72
        require_approval = require_approval_override or _bool_env("XHS_EXECUTOR_REQUIRE_APPROVAL", default=True)
        return ExecutorConfig(
            mode=mode,
            require_approval=require_approval,
            publish_hook_cmd=os.getenv("XHS_HOOK_PUBLISH_CMD", "").strip(),
            comment_hook_cmd=os.getenv("XHS_HOOK_COMMENT_CMD", "").strip(),
            publish_driver=os.getenv("XHS_PUBLISH_DRIVER", "auto").strip() or "auto",
            post_to_xhs_python_bin=os.getenv("XHS_PYTHON_BIN", "").strip() or sys.executable,
            post_to_xhs_script=Path(
                os.getenv(
                    "XHS_POST_TO_XHS_SCRIPT",
                    str(Path.home() / ".codex" / "skills" / "post-to-xhs" / "scripts" / "publish_pipeline.py"),
                ).strip()
            ),
            post_to_xhs_headless=_bool_env("XHS_POST_TO_XHS_HEADLESS", default=True),
            post_to_xhs_auto_publish=_bool_env("XHS_POST_TO_XHS_AUTO_PUBLISH", default=True),
            post_to_xhs_mode=os.getenv("XHS_POST_TO_XHS_MODE", "image-text").strip() or "image-text",
            post_to_xhs_account=os.getenv("XHS_POST_TO_XHS_ACCOUNT", "").strip(),
            session_check_cmd=os.getenv("XHS_HOOK_SESSION_CHECK_CMD", "").strip(),
            hook_timeout_sec=hook_timeout_sec,
            comments_per_topic=comments_per_topic,
            execution_dir=Path(
                os.getenv(
                    "XHS_EXECUTION_OUTPUT_DIR",
                    str(PROJECT_ROOT / "reports" / "xhs-ai-blogger" / "executions"),
                ).strip()
            ),
            account_service=os.getenv("XHS_ACCOUNT_KEYCHAIN_SERVICE", "feishu-bot-bridge.xhs.account").strip(),
            account_key=os.getenv("XHS_ACCOUNT_KEYCHAIN_ACCOUNT", "default").strip(),
            auth_mode=os.getenv("XHS_AUTH_MODE", "web_session").strip() or "web_session",
            session_max_age_hours=session_max_age_hours,
            session_check_required=_bool_env("XHS_SESSION_CHECK_REQUIRED", default=False),
            relogin_hint=os.getenv(
                "XHS_RELOGIN_HINT",
                "./scripts/xhs_web_session_auth.sh login --account-id <xhs_account_id> --username <login_name> --url https://creator.xiaohongshu.com/new/home",
            ).strip(),
        )


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _load_plan(path: Path) -> Dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("plan json must be an object")
    return parsed


def _load_account_payload(service: str, account: str) -> Dict[str, Any]:
    cmd = ["security", "find-generic-password", "-a", account, "-s", service, "-w"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        return {}
    raw = result.stdout.strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _render_command(template: str, values: Dict[str, str]) -> str:
    escaped = {key: shlex.quote(str(value)) for key, value in values.items()}
    try:
        return template.format(**escaped)
    except Exception as exc:
        raise RuntimeError(f"failed to render hook command: {exc}") from exc


def _run_shell(command: str, timeout_sec: int) -> Dict[str, Any]:
    timeout_value: Optional[int] = None if timeout_sec <= 0 else timeout_sec
    result = subprocess.run(
        ["/bin/bash", "-lc", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_value,
    )
    return {
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-800:],
        "stderr_tail": (result.stderr or "")[-800:],
    }


def _run_process(argv: List[str], timeout_sec: int) -> Dict[str, Any]:
    timeout_value: Optional[int] = None if timeout_sec <= 0 else timeout_sec
    result = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_value,
    )
    return {
        "returncode": result.returncode,
        "stdout_tail": (result.stdout or "")[-800:],
        "stderr_tail": (result.stderr or "")[-800:],
        "command": " ".join(shlex.quote(part) for part in argv),
    }


def _extract_actions(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    actions = plan.get("action_queue")
    if not isinstance(actions, list):
        return []
    return [item for item in actions if isinstance(item, dict) and item.get("type") in {"publish", "comment"}]


def _session_error_text(text: str) -> bool:
    lowered = text.lower()
    keywords = [
        "session expired",
        "login required",
        "unauthorized",
        "forbidden",
        "cookie invalid",
        "not logged in",
        "身份过期",
        "请登录",
        "登录失效",
    ]
    return any(keyword in lowered for keyword in keywords)


def _not_found_error_text(text: str) -> bool:
    lowered = text.lower()
    keywords = [
        "not_found:",
        "cannot find",
        "cannot switch to",
        "cannot open publish entry",
    ]
    return any(keyword in lowered for keyword in keywords)


def _check_web_session(cfg: ExecutorConfig, account_payload: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    account_id = _normalize_text(account_payload.get("account_id"))
    storage_state = _normalize_text(account_payload.get("storage_state"))
    if not account_id:
        return {
            "ok": False,
            "reason": "keychain missing account_id",
            "status": "session_expired",
            "relogin_hint": cfg.relogin_hint,
        }
    if not storage_state:
        return {
            "ok": False,
            "reason": "keychain missing storage_state (web auth session file)",
            "status": "session_expired",
            "relogin_hint": cfg.relogin_hint,
        }

    state_path = Path(storage_state).expanduser()
    if not state_path.exists():
        return {
            "ok": False,
            "reason": f"storage_state file not found: {state_path}",
            "status": "session_expired",
            "relogin_hint": cfg.relogin_hint,
        }

    updated_at = account_payload.get("session_updated_at")
    if updated_at:
        try:
            updated_dt = dt.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=dt.timezone.utc)
            age_hours = (dt.datetime.now(dt.timezone.utc) - updated_dt).total_seconds() / 3600.0
        except Exception:
            age_hours = (dt.datetime.now().timestamp() - state_path.stat().st_mtime) / 3600.0
    else:
        age_hours = (dt.datetime.now().timestamp() - state_path.stat().st_mtime) / 3600.0

    if age_hours > cfg.session_max_age_hours:
        return {
            "ok": False,
            "reason": f"web session age {age_hours:.1f}h exceeds limit {cfg.session_max_age_hours}h",
            "status": "session_expired",
            "relogin_hint": cfg.relogin_hint,
        }

    if cfg.session_check_required and cfg.session_check_cmd and not dry_run:
        render_values = {
            "account_id": account_id,
            "username": _normalize_text(account_payload.get("username")),
            "storage_state": str(state_path),
        }
        command = _render_command(cfg.session_check_cmd, render_values)
        run_result = _run_shell(command, timeout_sec=cfg.hook_timeout_sec)
        if int(run_result["returncode"]) != 0:
            reason = _normalize_text(run_result["stderr_tail"] or run_result["stdout_tail"] or "session check failed")
            return {
                "ok": False,
                "reason": reason,
                "status": "session_expired",
                "relogin_hint": cfg.relogin_hint,
            }

    return {"ok": True, "reason": "", "status": "ok", "relogin_hint": ""}


def _resolve_publish_driver(cfg: ExecutorConfig) -> str:
    value = _normalize_text(cfg.publish_driver).lower()
    if value in {"command_hooks", "hooks", "hook"}:
        return "command_hooks"
    if value in {"post_to_xhs", "post-to-xhs", "post_to_xhs_skill", "skill"}:
        return "post_to_xhs"
    if value in {"auto", ""}:
        return "post_to_xhs" if cfg.post_to_xhs_script.expanduser().exists() else "command_hooks"
    raise RuntimeError(f"unsupported publish driver: {cfg.publish_driver}")


def _resolve_publish_mode(cfg: ExecutorConfig, payload: Dict[str, Any]) -> str:
    payload_mode = _normalize_text(payload.get("publish_mode")).lower()
    default_mode = _normalize_text(cfg.post_to_xhs_mode).lower()
    value = payload_mode or default_mode
    if value in {"long_article", "long-article", "article", "long"}:
        return "long-article"
    return "image-text"


def _run_post_to_xhs_publish(
    cfg: ExecutorConfig,
    topic: str,
    payload: Dict[str, Any],
    images_list: List[str],
    timeout_sec: int,
) -> Dict[str, Any]:
    script_path = cfg.post_to_xhs_script.expanduser()
    if not script_path.exists():
        raise RuntimeError(f"post-to-xhs publish script not found: {script_path}")

    title = _normalize_text(payload.get("title")) or topic
    content = str(payload.get("content") or "").strip()
    if not title:
        raise RuntimeError("publish title is empty")
    if not content:
        raise RuntimeError("publish content is empty")

    mode = _resolve_publish_mode(cfg, payload)
    if mode == "image-text" and not images_list:
        raise RuntimeError("publish action missing images for post-to-xhs image-text mode")

    argv: List[str] = [cfg.post_to_xhs_python_bin, str(script_path), "--mode", mode, "--title", title, "--content", content]
    if images_list:
        argv.extend(["--images", *images_list])
    if cfg.post_to_xhs_headless:
        argv.append("--headless")
    if cfg.post_to_xhs_auto_publish and mode == "image-text":
        argv.append("--auto-publish")
    if cfg.post_to_xhs_account:
        argv.extend(["--account", cfg.post_to_xhs_account])

    return _run_process(argv, timeout_sec=timeout_sec)


def _execute_actions(
    cfg: ExecutorConfig,
    actions: List[Dict[str, Any]],
    account_payload: Dict[str, Any],
    dry_run: bool,
    approve: bool,
) -> Dict[str, Any]:
    publish_total = sum(1 for item in actions if item.get("type") == "publish")
    raw_comment_total = sum(1 for item in actions if item.get("type") == "comment")
    comment_total = raw_comment_total
    publish_success = 0
    comment_success = 0
    has_not_found_failure = False
    records: List[Dict[str, Any]] = []

    if cfg.require_approval and not approve:
        return {
            "status": "pending_approval",
            "mode": cfg.mode,
            "publish_driver": cfg.publish_driver,
            "publish_total": publish_total,
            "publish_success": 0,
            "comment_total": comment_total,
            "comment_success": 0,
            "message": "execution skipped: approval required",
            "records": records,
        }

    account_id = _normalize_text(account_payload.get("account_id"))
    username = _normalize_text(account_payload.get("username"))
    storage_state = _normalize_text(account_payload.get("storage_state"))

    if cfg.mode == "queue_only":
        return {
            "status": "queued_only",
            "mode": cfg.mode,
            "publish_driver": cfg.publish_driver,
            "publish_total": publish_total,
            "publish_success": 0 if not dry_run else publish_total,
            "comment_total": comment_total,
            "comment_success": 0 if not dry_run else comment_total,
            "message": "queue_only mode: no external publish/comment action executed",
            "records": records,
        }

    if cfg.mode != "command_hooks":
        return {
            "status": "failed",
            "mode": cfg.mode,
            "publish_driver": cfg.publish_driver,
            "publish_total": publish_total,
            "publish_success": 0,
            "comment_total": comment_total,
            "comment_success": 0,
            "message": f"unsupported executor mode: {cfg.mode}",
            "records": records,
        }

    try:
        publish_driver = _resolve_publish_driver(cfg)
    except Exception as exc:
        return {
            "status": "failed",
            "mode": cfg.mode,
            "publish_driver": cfg.publish_driver,
            "publish_total": publish_total,
            "publish_success": 0,
            "comment_total": comment_total,
            "comment_success": 0,
            "message": str(exc),
            "records": records,
        }

    if publish_driver == "command_hooks":
        if not cfg.publish_hook_cmd or not cfg.comment_hook_cmd:
            return {
                "status": "failed",
                "mode": cfg.mode,
                "publish_driver": publish_driver,
                "publish_total": publish_total,
                "publish_success": 0,
                "comment_total": comment_total,
                "comment_success": 0,
                "message": "command_hooks driver requires XHS_HOOK_PUBLISH_CMD and XHS_HOOK_COMMENT_CMD",
                "records": records,
            }
    elif publish_driver == "post_to_xhs":
        if not cfg.post_to_xhs_script.expanduser().exists():
            return {
                "status": "failed",
                "mode": cfg.mode,
                "publish_driver": publish_driver,
                "publish_total": publish_total,
                "publish_success": 0,
                "comment_total": comment_total,
                "comment_success": 0,
                "message": f"post-to-xhs script missing: {cfg.post_to_xhs_script}",
                "records": records,
            }
        if not cfg.comment_hook_cmd:
            comment_total = 0

    needs_web_session = cfg.auth_mode == "web_session" and (
        publish_driver == "command_hooks" or bool(cfg.comment_hook_cmd)
    )
    if needs_web_session:
        session_check = _check_web_session(cfg, account_payload, dry_run=dry_run)
        if not session_check.get("ok"):
            return {
                "status": "session_expired",
                "mode": cfg.mode,
                "publish_driver": publish_driver,
                "publish_total": publish_total,
                "publish_success": 0,
                "comment_total": comment_total,
                "comment_success": 0,
                "message": session_check.get("reason") or "web session expired",
                "relogin_hint": session_check.get("relogin_hint", cfg.relogin_hint),
                "records": records,
            }

    for action in actions:
        action_type = action.get("type")
        topic = _normalize_text(action.get("topic"))
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        images = payload.get("images") if isinstance(payload.get("images"), list) else action.get("images")
        images_list = [str(item).strip() for item in (images or []) if str(item).strip()]
        images_csv = ",".join(images_list)
        values = {
            "account_id": account_id,
            "username": username,
            "topic": topic,
            "title": _normalize_text(payload.get("title")),
            "title_alt": _normalize_text(payload.get("title_alt")),
            "content": str(payload.get("content") or ""),
            "tags_csv": ",".join(payload.get("tags") or []),
            "comment": _normalize_text(payload.get("comment")),
            "target_profile_hint": _normalize_text(payload.get("target_profile_hint")),
            "storage_state": storage_state,
            "images_csv": images_csv,
        }
        if action_type == "publish":
            if publish_driver == "command_hooks" and not images_list:
                records.append(
                    {
                        "action_id": action.get("action_id"),
                        "type": action_type,
                        "topic": topic,
                        "status": "failed",
                        "error": "publish action missing images",
                    }
                )
                continue
            template = cfg.publish_hook_cmd
        elif action_type == "comment":
            if publish_driver == "post_to_xhs" and not cfg.comment_hook_cmd:
                records.append(
                    {
                        "action_id": action.get("action_id"),
                        "type": action_type,
                        "topic": topic,
                        "status": "skipped_no_hook",
                        "reason": "comment hook not configured for post-to-xhs driver",
                    }
                )
                continue
            template = cfg.comment_hook_cmd
        else:
            continue

        try:
            rendered_cmd = ""
            run_result: Dict[str, Any]
            if action_type == "publish" and publish_driver == "post_to_xhs":
                mode_value = _resolve_publish_mode(cfg, payload)
                dry_cmd = [
                    cfg.post_to_xhs_python_bin,
                    str(cfg.post_to_xhs_script.expanduser()),
                    "--mode",
                    mode_value,
                    "--title",
                    values["title"] or topic,
                    "--content",
                    values["content"],
                ]
                if images_list:
                    dry_cmd.extend(["--images", *images_list])
                if cfg.post_to_xhs_headless:
                    dry_cmd.append("--headless")
                if cfg.post_to_xhs_auto_publish and mode_value == "image-text":
                    dry_cmd.append("--auto-publish")
                if cfg.post_to_xhs_account:
                    dry_cmd.extend(["--account", cfg.post_to_xhs_account])
                rendered_cmd = " ".join(shlex.quote(part) for part in dry_cmd)
                if dry_run:
                    run_result = {"returncode": 0, "stdout_tail": "", "stderr_tail": "", "command": rendered_cmd}
                else:
                    run_result = _run_post_to_xhs_publish(
                        cfg=cfg,
                        topic=topic,
                        payload=payload,
                        images_list=images_list,
                        timeout_sec=cfg.hook_timeout_sec,
                    )
            else:
                rendered_cmd = _render_command(template, values)
                if dry_run:
                    run_result = {"returncode": 0, "stdout_tail": "", "stderr_tail": "", "command": rendered_cmd}
                else:
                    run_result = _run_shell(rendered_cmd, cfg.hook_timeout_sec)

            if dry_run:
                records.append(
                    {
                        "action_id": action.get("action_id"),
                        "type": action_type,
                        "topic": topic,
                        "status": "dry_run_skipped",
                        "command": rendered_cmd,
                    }
                )
                if action_type == "publish":
                    publish_success += 1
                else:
                    comment_success += 1
                continue

            ok = int(run_result["returncode"]) == 0
            stderr_tail = run_result["stderr_tail"]
            stdout_tail = run_result["stdout_tail"]
            merged = f"{stderr_tail}\n{stdout_tail}"
            record_status = "success" if ok else "failed"
            if (not ok) and _not_found_error_text(merged):
                has_not_found_failure = True
                record_status = "failed_not_found"
            records.append(
                {
                    "action_id": action.get("action_id"),
                    "type": action_type,
                    "topic": topic,
                    "status": record_status,
                    "command": str(run_result.get("command") or rendered_cmd),
                    "stdout_tail": stdout_tail,
                    "stderr_tail": stderr_tail,
                }
            )
            if ok:
                if action_type == "publish":
                    publish_success += 1
                else:
                    comment_success += 1
            else:
                if _session_error_text(merged):
                    return {
                        "status": "session_expired",
                        "mode": cfg.mode,
                        "publish_driver": publish_driver,
                        "publish_total": publish_total,
                        "publish_success": publish_success,
                        "comment_total": comment_total,
                        "comment_success": comment_success,
                        "message": "executor hook reported login/session expired",
                        "relogin_hint": cfg.relogin_hint,
                        "records": records,
                    }
        except Exception as exc:
            error_text = _normalize_text(exc)
            if _session_error_text(error_text):
                return {
                    "status": "session_expired",
                    "mode": cfg.mode,
                    "publish_driver": publish_driver,
                    "publish_total": publish_total,
                    "publish_success": publish_success,
                    "comment_total": comment_total,
                    "comment_success": comment_success,
                    "message": error_text,
                    "relogin_hint": cfg.relogin_hint,
                    "records": records,
                }
            record_status = "failed_not_found" if _not_found_error_text(error_text) else "failed"
            if record_status == "failed_not_found":
                has_not_found_failure = True
            records.append(
                {
                    "action_id": action.get("action_id"),
                    "type": action_type,
                    "topic": topic,
                    "status": record_status,
                    "error": error_text,
                }
            )

    if has_not_found_failure:
        status = "failed_not_found"
    elif publish_success == publish_total and comment_success == comment_total:
        status = "success"
    else:
        status = "partial_success"
    return {
        "status": status,
        "mode": cfg.mode,
        "publish_driver": publish_driver,
        "publish_total": publish_total,
        "publish_success": publish_success,
        "comment_total": comment_total,
        "comment_success": comment_success,
        "message": "execution completed",
        "no_retry_policy": "No Retry Policy: find-error fail-fast enabled",
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute Xiaohongshu automation queue from generated report plan.")
    parser.add_argument("--plan-json", required=True, help="Path to generated plan json from xhs_ai_blogger_job")
    parser.add_argument("--output", help="Execution result output path")
    parser.add_argument("--mode", default="", help="Executor mode override: queue_only | command_hooks")
    parser.add_argument("--require-approval", action="store_true", help="Force approval gate")
    parser.add_argument("--approve", action="store_true", help="Approve execution for current run")
    parser.add_argument("--dry-run", action="store_true", help="Do not run external hooks; only simulate")
    args = parser.parse_args()

    cfg = ExecutorConfig.from_env(mode_override=args.mode, require_approval_override=args.require_approval)
    plan_path = Path(args.plan_json)
    plan = _load_plan(plan_path)
    actions = _extract_actions(plan)
    account_payload = _load_account_payload(cfg.account_service, cfg.account_key)

    result = _execute_actions(
        cfg=cfg,
        actions=actions,
        account_payload=account_payload,
        dry_run=args.dry_run,
        approve=args.approve,
    )
    result["report_date"] = _normalize_text(plan.get("report_date")) or dt.date.today().isoformat()
    result["plan_json"] = str(plan_path)
    result["account_id"] = _normalize_text(account_payload.get("account_id"))
    result["account_username"] = _normalize_text(account_payload.get("username"))
    result["storage_state"] = _normalize_text(account_payload.get("storage_state"))

    if args.output:
        out_path = Path(args.output)
    else:
        cfg.execution_dir.mkdir(parents=True, exist_ok=True)
        out_path = cfg.execution_dir / f"{result['report_date']}.execution.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"execution_result_file={out_path}")
    print(f"status={result['status']}")
    print(f"publish_driver={result.get('publish_driver', cfg.publish_driver)}")
    print(f"publish={result['publish_success']}/{result['publish_total']}")
    print(f"comment={result['comment_success']}/{result['comment_total']}")

    if result["status"] in {"failed", "session_expired", "failed_not_found"}:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[xhs-auto-executor] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
