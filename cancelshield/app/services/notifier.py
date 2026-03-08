from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from app.db import get_conn


def _build_payload(
    provider: str,
    title: str,
    message: str,
    metadata: Optional[Dict[str, Any]],
) -> bytes:
    if provider == "feishu":
        body = {
            "msg_type": "text",
            "content": {"text": f"{title}\n{message}"},
        }
    elif provider == "slack":
        body = {"text": f"*{title}*\n{message}"}
    else:
        body = {
            "title": title,
            "message": message,
            "metadata": metadata or {},
        }
    return json.dumps(body).encode("utf-8")


def _post_webhook(provider: str, webhook_url: str, payload: bytes) -> tuple[bool, str]:
    req = urllib.request.Request(
        url=webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:  # nosec B310
            detail = f"http_status={response.status}"
            return 200 <= response.status < 300, detail
    except urllib.error.URLError as exc:
        return False, f"url_error={exc}"
    except Exception as exc:  # pragma: no cover - defensive guard
        return False, f"error={exc}"


def dispatch_team_notification(
    team_id: int,
    event_type: str,
    title: str,
    message: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> tuple[int, int, int]:
    attempted = 0
    sent = 0
    failed = 0

    with get_conn() as conn:
        channels = conn.execute(
            """
            SELECT id, provider, webhook_url
            FROM notification_channels
            WHERE team_id = ? AND enabled = 1
            ORDER BY id ASC
            """,
            (team_id,),
        ).fetchall()

        for channel in channels:
            attempted += 1
            provider = str(channel["provider"])
            webhook_url = str(channel["webhook_url"])
            payload = _build_payload(provider, title, message, metadata)
            ok, detail = _post_webhook(provider, webhook_url, payload)
            status = "sent" if ok else "failed"
            if ok:
                sent += 1
            else:
                failed += 1

            conn.execute(
                """
                INSERT INTO notification_events (
                    team_id, provider, event_type, status, detail
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (team_id, provider, event_type, status, detail),
            )

    return attempted, sent, failed
