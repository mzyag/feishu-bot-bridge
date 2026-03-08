from __future__ import annotations

import base64
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient

from app.main import app

BASE_DIR = Path(__file__).resolve().parents[1]


class SmokeError(RuntimeError):
    pass


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def _request(
    client: TestClient,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    response = client.request(method=method, url=path, json=payload, headers=headers)
    if response.status_code >= 400:
        raise SmokeError(f"HTTP {response.status_code} {method} {path}: {response.text}")

    if not response.text:
        return None

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise SmokeError(f"invalid JSON response for {method} {path}: {response.text}") from exc


def run() -> None:
    now_tag = int(time.time())
    team_name = f"Smoke Team {now_tag}"
    owner_email = f"owner+{now_tag}@example.com"

    with TestClient(app) as client:
        health = _request(client, "GET", "/health")
        _assert(health.get("status") == "ok", "health status invalid")
        print("[1/12] health check ok")

        bootstrap = _request(
            client,
            "POST",
            "/api/v1/teams/bootstrap",
            payload={"name": team_name, "owner_email": owner_email},
        )
        admin_key = bootstrap["api_key"]
        _assert(admin_key.startswith("cs_live_"), "bootstrap api_key format invalid")
        print("[2/12] team bootstrap ok")

        admin_headers = {"X-API-Key": admin_key}

        team_me = _request(client, "GET", "/api/v1/teams/me", headers=admin_headers)
        _assert(team_me["role"] == "admin", "admin key role mismatch")
        print("[3/12] team me ok")

        member_email = f"editor+{now_tag}@example.com"
        member = _request(
            client,
            "POST",
            "/api/v1/teams/members",
            headers=admin_headers,
            payload={"email": member_email, "role": "editor"},
        )
        _assert(member["email"] == member_email, "member create failed")
        print("[4/12] member create ok")

        editor_key_obj = _request(
            client,
            "POST",
            "/api/v1/teams/api-keys",
            headers=admin_headers,
            payload={
                "label": "smoke-editor",
                "role": "editor",
                "created_by_email": owner_email,
            },
        )
        editor_key = editor_key_obj["api_key"]
        editor_headers = {"X-API-Key": editor_key}
        print("[5/12] editor api key issue ok")

        _request(
            client,
            "POST",
            "/api/v1/notifications/channels",
            headers=admin_headers,
            payload={
                "provider": "generic",
                "webhook_url": "http://127.0.0.1:9/cancelshield-hook",
                "enabled": True,
            },
        )
        print("[6/12] notification channel save ok")

        renewal_date = (date.today() + timedelta(days=1)).isoformat()
        subscription = _request(
            client,
            "POST",
            "/api/v1/subscriptions",
            headers=editor_headers,
            payload={
                "vendor": "Notion",
                "plan_name": "Team",
                "amount": 29,
                "currency": "USD",
                "renewal_date": renewal_date,
                "owner_email": owner_email,
                "notes": "smoke test subscription",
            },
        )
        sub_id = int(subscription["id"])
        print("[7/12] subscription create ok")

        subs = _request(client, "GET", "/api/v1/subscriptions", headers=editor_headers)
        _assert(any(int(item["id"]) == sub_id for item in subs), "subscription list missing new id")
        print("[8/12] subscription list ok")

        _request(
            client,
            "POST",
            f"/api/v1/subscriptions/{sub_id}/evidence",
            headers=editor_headers,
            payload={
                "event_type": "cancel_attempt",
                "actor": owner_email,
                "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                "details": "json evidence from smoke",
            },
        )

        uploaded = _request(
            client,
            "POST",
            f"/api/v1/subscriptions/{sub_id}/evidence/upload",
            headers=editor_headers,
            payload={
                "actor": owner_email,
                "event_type": "cancel_attempt",
                "details": "file evidence from smoke",
                "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                "file_name": "smoke-evidence.txt",
                "file_content_base64": base64.b64encode(
                    b"cancelshield smoke evidence file"
                ).decode("ascii"),
            },
        )
        _assert("screenshot_path" in uploaded, "evidence upload response invalid")
        print("[9/12] evidence json + file upload ok")

        exported = _request(
            client,
            "POST",
            f"/api/v1/subscriptions/{sub_id}/dispute-export",
            headers=editor_headers,
        )
        export_path = Path(exported["export_path"])
        _assert(export_path.exists(), f"export file missing: {export_path}")
        print("[10/12] dispute export ok")

        test_push = _request(client, "POST", "/api/v1/notifications/test", headers=editor_headers)
        _assert(test_push["attempted"] >= 1, "notification test attempted should be >= 1")
        print("[11/12] notification test ok")

        reminders = _request(client, "POST", "/api/v1/reminders/run", headers=editor_headers)
        _assert(reminders["queued_count"] >= 1, "reminder run should queue at least one item")
        print("[12/12] reminder run ok")

        print("SMOKE TEST PASSED")
        print(f"team={team_name} subscription_id={sub_id} export={export_path}")


if __name__ == "__main__":
    run()
