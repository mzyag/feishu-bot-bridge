from __future__ import annotations

from fastapi import APIRouter, Depends

from app.db import get_conn
from app.schemas import (
    NotificationChannelCreate,
    NotificationChannelOut,
    NotificationTestOut,
)
from app.security import AuthContext, require_api_key, require_roles
from app.services.notifier import dispatch_team_notification

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("/channels", response_model=list[NotificationChannelOut])
def list_channels(auth: AuthContext = Depends(require_api_key)) -> list[NotificationChannelOut]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, provider, webhook_url, enabled
            FROM notification_channels
            WHERE team_id = ?
            ORDER BY id ASC
            """,
            (auth.team_id,),
        ).fetchall()
    return [
        NotificationChannelOut(
            id=int(row["id"]),
            provider=str(row["provider"]),
            webhook_url=str(row["webhook_url"]),
            enabled=bool(row["enabled"]),
        )
        for row in rows
    ]


@router.post("/channels", response_model=NotificationChannelOut, status_code=201)
def upsert_channel(
    payload: NotificationChannelCreate,
    auth: AuthContext = Depends(require_roles("admin", "editor")),
) -> NotificationChannelOut:
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT id FROM notification_channels
            WHERE team_id = ? AND provider = ?
            """,
            (auth.team_id, payload.provider),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE notification_channels
                SET webhook_url = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (payload.webhook_url, int(payload.enabled), int(existing["id"])),
            )
            channel_id = int(existing["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO notification_channels (team_id, provider, webhook_url, enabled)
                VALUES (?, ?, ?, ?)
                """,
                (auth.team_id, payload.provider, payload.webhook_url, int(payload.enabled)),
            )
            channel_id = int(cur.lastrowid)

        row = conn.execute(
            """
            SELECT id, provider, webhook_url, enabled
            FROM notification_channels
            WHERE id = ?
            """,
            (channel_id,),
        ).fetchone()

    return NotificationChannelOut(
        id=int(row["id"]),
        provider=str(row["provider"]),
        webhook_url=str(row["webhook_url"]),
        enabled=bool(row["enabled"]),
    )


@router.post("/test", response_model=NotificationTestOut)
def test_notification(auth: AuthContext = Depends(require_api_key)) -> NotificationTestOut:
    attempted, sent, failed = dispatch_team_notification(
        team_id=auth.team_id,
        event_type="manual_test",
        title="CancelShield Test",
        message="this is a manual test message from console",
        metadata={"team_id": auth.team_id},
    )
    return NotificationTestOut(attempted=attempted, sent=sent, failed=failed)
