#!/usr/bin/env python3
import argparse
import datetime as dt
import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from opportunity_scout_job import (
    DEFAULT_JOB_LOCK_FILE,
    Config,
    PROJECT_ROOT,
    run as run_scout,
    send_to_feishu,
)


DEFAULT_STATE_FILE = PROJECT_ROOT / ".state" / "opportunity_scout_watchdog.json"
DEFAULT_LOCK_FILE = PROJECT_ROOT / ".state" / "opportunity_scout_watchdog.lock"


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def _state_file_path() -> Path:
    raw = os.getenv("SCOUT_WATCHDOG_STATE_FILE", str(DEFAULT_STATE_FILE)).strip()
    return Path(raw) if raw else DEFAULT_STATE_FILE


def _lock_file_path() -> Path:
    return DEFAULT_LOCK_FILE


def _job_lock_file_path() -> Path:
    raw = os.getenv("SCOUT_JOB_LOCK_FILE", str(DEFAULT_JOB_LOCK_FILE)).strip()
    return Path(raw) if raw else DEFAULT_JOB_LOCK_FILE


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"days": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"days": {}}
    if not isinstance(data, dict):
        return {"days": {}}
    days = data.get("days")
    if not isinstance(days, dict):
        data["days"] = {}
    return data


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    days = state.get("days", {})
    if isinstance(days, dict):
        keys = sorted(days.keys())
        if len(keys) > 14:
            to_remove = set(keys[:-14])
            for key in to_remove:
                days.pop(key, None)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_handle.close()
        return None
    return lock_handle


def _report_paths(cfg: Config, report_date: str) -> Tuple[Path, Path]:
    return cfg.output_dir / f"{report_date}.md", cfg.output_dir / f"{report_date}.json"


def _has_report_outputs(cfg: Config, report_date: str) -> bool:
    markdown_path, json_path = _report_paths(cfg, report_date)
    return markdown_path.exists() and json_path.exists()


def _is_job_running() -> bool:
    lock_path = _job_lock_file_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    return False


def _schedule_due(report_date: str) -> bool:
    today = dt.date.today().isoformat()
    if report_date < today:
        return True
    if report_date > today:
        return False
    hour = _env_int("SCOUT_REPORT_HOUR", 8, 0, 23)
    minute = _env_int("SCOUT_REPORT_MINUTE", 0, 0, 59)
    grace = _env_int("SCOUT_WATCHDOG_GRACE_MIN", 20, 0, 180)
    now = dt.datetime.now()
    due_at = dt.datetime.combine(now.date(), dt.time(hour=hour, minute=minute)) + dt.timedelta(minutes=grace)
    return now >= due_at


def _watchdog_codex_timeout_sec() -> int:
    return _env_int("SCOUT_WATCHDOG_CODEX_TIMEOUT_SEC", 900, 60, 7200)


def _send_failure_message(cfg: Config, report_date: str, err_text: str, dry_run: bool) -> None:
    body = (
        "🚨 Opportunity Scout Watchdog\n"
        f"Date: {report_date}\n"
        "Status: missed scheduled run, rerun failed.\n"
        f"Error: {err_text}\n"
        "Action: run `./scripts/launchd_opportunity_scout.sh run-now` to retry manually."
    )
    send_to_feishu(cfg, f"【FAILED】{report_date}", body, dry_run=dry_run)


def run_watchdog(report_date: str, dry_run: bool) -> Dict[str, Any]:
    cfg = Config.from_env()
    state_path = _state_file_path()
    state = _load_state(state_path)
    days = state.setdefault("days", {})
    day_state = days.setdefault(report_date, {})
    now = dt.datetime.now().isoformat(timespec="seconds")
    markdown_path, json_path = _report_paths(cfg, report_date)

    day_state["last_check_at"] = now
    day_state["markdown_file"] = str(markdown_path)
    day_state["json_file"] = str(json_path)

    if _has_report_outputs(cfg, report_date):
        day_state["status"] = "ok"
        _save_state(state_path, state)
        return {"action": "noop_report_exists", "report_date": report_date}

    if not _schedule_due(report_date):
        day_state["status"] = "waiting_schedule_window"
        _save_state(state_path, state)
        return {"action": "noop_not_due", "report_date": report_date}

    if _is_job_running():
        day_state["status"] = "job_running_skip"
        _save_state(state_path, state)
        return {"action": "noop_job_running", "report_date": report_date}

    if day_state.get("rerun_attempted") is True:
        day_state["status"] = "already_rerun_attempted"
        _save_state(state_path, state)
        return {"action": "noop_already_attempted", "report_date": report_date}

    day_state["rerun_attempted"] = True
    day_state["rerun_at"] = now
    _save_state(state_path, state)

    if dry_run:
        day_state["status"] = "dry_run_would_rerun"
        _save_state(state_path, state)
        return {"action": "dry_run_would_rerun", "report_date": report_date}

    previous_timeout = os.getenv("SCOUT_CODEX_TIMEOUT_SEC")
    try:
        existing_timeout_raw = (previous_timeout or "").strip()
        try:
            existing_timeout = int(existing_timeout_raw) if existing_timeout_raw else 0
        except ValueError:
            existing_timeout = 0
        if existing_timeout <= 0:
            os.environ["SCOUT_CODEX_TIMEOUT_SEC"] = str(_watchdog_codex_timeout_sec())

        result = run_scout(report_date=report_date, dry_run=False, mock_hunt_file=None, mock_report_file=None)
        day_state["status"] = "rerun_success"
        day_state["rerun_success"] = True
        day_state["selected_score"] = result.get("selected_score")
        _save_state(state_path, state)
        return {"action": "rerun_success", "report_date": report_date, "selected_score": result.get("selected_score")}
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {exc}"
        if "already running" in err_text.lower():
            day_state["status"] = "job_running_skip"
            day_state["rerun_attempted"] = False
            day_state.pop("rerun_at", None)
            _save_state(state_path, state)
            return {"action": "noop_job_running", "report_date": report_date}
        day_state["status"] = "rerun_failed"
        day_state["rerun_success"] = False
        day_state["error"] = err_text[:1500]
        _save_state(state_path, state)

        if not day_state.get("failure_notified"):
            try:
                _send_failure_message(cfg, report_date, err_text[:500], dry_run=False)
                day_state["failure_notified"] = True
            except Exception as notify_exc:
                day_state["failure_notify_error"] = f"{type(notify_exc).__name__}: {notify_exc}"[:500]
            _save_state(state_path, state)
        raise
    finally:
        if previous_timeout is None:
            os.environ.pop("SCOUT_CODEX_TIMEOUT_SEC", None)
        else:
            os.environ["SCOUT_CODEX_TIMEOUT_SEC"] = previous_timeout


def main() -> None:
    parser = argparse.ArgumentParser(description="Watchdog for opportunity_scout scheduled run.")
    parser.add_argument("--date", help="Report date in YYYY-MM-DD format. Default: today.")
    parser.add_argument("--dry-run", action="store_true", help="Check and simulate rerun decision only.")
    args = parser.parse_args()

    report_date = args.date or dt.date.today().isoformat()
    lock_handle = _acquire_lock(_lock_file_path())
    if lock_handle is None:
        print("watchdog_skip=already_running")
        return
    try:
        result = run_watchdog(report_date=report_date, dry_run=args.dry_run)
    except Exception as exc:
        print(f"[watchdog] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        except Exception:
            pass

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
