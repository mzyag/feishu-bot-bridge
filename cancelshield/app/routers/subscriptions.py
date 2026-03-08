from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_conn
from app.schemas import (
    DisputeExportOut,
    EvidenceCreate,
    EvidenceOut,
    EvidenceUploadCreate,
    ReminderPreview,
    SubscriptionCreate,
    SubscriptionOut,
)
from app.security import AuthContext, require_api_key, require_roles
from app.services.exporter import build_dispute_export
from app.services.reminder import build_reminder_schedule

router = APIRouter(prefix="/api/v1/subscriptions", tags=["subscriptions"])
BASE_DIR = Path(__file__).resolve().parents[2]
EXPORT_DIR = BASE_DIR / "exports"
EVIDENCE_DIR = BASE_DIR / "data" / "evidence"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def _get_subscription_or_404(subscription_id: int, team_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE id = ? AND team_id = ?
            """,
            (subscription_id, team_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return dict(row)


def _save_upload(subscription_id: int, file_name: str, file_bytes: bytes) -> str:
    ext = Path(file_name).suffix.lower() or ".bin"
    safe_name = f"{subscription_id}-{uuid4().hex}{ext}"
    target_dir = EVIDENCE_DIR / str(subscription_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name

    with target_path.open("wb") as out:
        out.write(file_bytes)

    return str(target_path.relative_to(BASE_DIR))


@router.post("", response_model=SubscriptionOut, status_code=201)
def create_subscription(
    payload: SubscriptionCreate,
    auth: AuthContext = Depends(require_roles("admin", "editor")),
) -> SubscriptionOut:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO subscriptions (
                team_id, team_name, vendor, plan_name, amount, currency,
                renewal_date, owner_email, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                auth.team_id,
                auth.team_name,
                payload.vendor,
                payload.plan_name,
                payload.amount,
                payload.currency.upper(),
                payload.renewal_date.isoformat(),
                payload.owner_email,
                payload.notes,
            ),
        )
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (new_id,)).fetchone()

    return SubscriptionOut(**dict(row))


@router.get("", response_model=list[SubscriptionOut])
def list_subscriptions(auth: AuthContext = Depends(require_api_key)) -> list[SubscriptionOut]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE team_id = ?
            ORDER BY renewal_date ASC
            """,
            (auth.team_id,),
        ).fetchall()
    return [SubscriptionOut(**dict(row)) for row in rows]


@router.post("/{subscription_id}/evidence", response_model=EvidenceOut, status_code=201)
def create_evidence(
    subscription_id: int,
    payload: EvidenceCreate,
    auth: AuthContext = Depends(require_roles("admin", "editor")),
) -> EvidenceOut:
    _get_subscription_or_404(subscription_id, auth.team_id)

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO evidence_events (
                subscription_id, event_type, actor, occurred_at,
                page_url, screenshot_path, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subscription_id,
                payload.event_type,
                payload.actor,
                payload.occurred_at.isoformat(),
                payload.page_url,
                payload.screenshot_path,
                payload.details,
            ),
        )
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM evidence_events WHERE id = ?", (new_id,)).fetchone()

    return EvidenceOut(**dict(row))


@router.post("/{subscription_id}/evidence/upload", response_model=EvidenceOut, status_code=201)
def upload_evidence(
    subscription_id: int,
    payload: EvidenceUploadCreate,
    auth: AuthContext = Depends(require_roles("admin", "editor")),
) -> EvidenceOut:
    _get_subscription_or_404(subscription_id, auth.team_id)
    try:
        file_bytes = base64.b64decode(payload.file_content_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 file content") from exc
    screenshot_path = _save_upload(subscription_id, payload.file_name, file_bytes)

    occurred_dt = datetime.now(timezone.utc)
    if payload.occurred_at:
        occurred_dt = payload.occurred_at

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO evidence_events (
                subscription_id, event_type, actor, occurred_at,
                page_url, screenshot_path, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subscription_id,
                payload.event_type,
                payload.actor,
                occurred_dt.isoformat(),
                payload.page_url,
                screenshot_path,
                payload.details,
            ),
        )
        new_id = cur.lastrowid
        row = conn.execute("SELECT * FROM evidence_events WHERE id = ?", (new_id,)).fetchone()

    return EvidenceOut(**dict(row))


@router.get("/{subscription_id}/reminders/preview", response_model=ReminderPreview)
def preview_reminders(
    subscription_id: int,
    auth: AuthContext = Depends(require_api_key),
) -> ReminderPreview:
    subscription = _get_subscription_or_404(subscription_id, auth.team_id)
    renewal_date = date.fromisoformat(subscription["renewal_date"])
    return ReminderPreview(
        subscription_id=subscription_id,
        reminder_dates=build_reminder_schedule(renewal_date),
    )


@router.post("/{subscription_id}/dispute-export", response_model=DisputeExportOut)
def export_dispute_case(
    subscription_id: int,
    auth: AuthContext = Depends(require_api_key),
) -> DisputeExportOut:
    subscription = _get_subscription_or_404(subscription_id, auth.team_id)

    with get_conn() as conn:
        evidence_rows = conn.execute(
            """
            SELECT id, subscription_id, event_type, actor, occurred_at,
                   page_url, screenshot_path, details
            FROM evidence_events
            WHERE subscription_id = ?
            ORDER BY occurred_at ASC
            """,
            (subscription_id,),
        ).fetchall()

    evidence_payload = []
    for row in evidence_rows:
        item = dict(row)
        # Standardize time string for downstream dispute packages.
        try:
            item["occurred_at"] = datetime.fromisoformat(item["occurred_at"]).isoformat()
        except (TypeError, ValueError):
            pass
        evidence_payload.append(item)

    archive = build_dispute_export(
        export_dir=EXPORT_DIR,
        subscription=subscription,
        evidence_rows=evidence_payload,
    )

    return DisputeExportOut(
        subscription_id=subscription_id,
        export_path=str(archive),
        evidence_count=len(evidence_payload),
    )
