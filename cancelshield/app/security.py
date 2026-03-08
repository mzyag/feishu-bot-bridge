from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.db import get_conn

VALID_ROLES = {"admin", "editor", "viewer"}


@dataclass(frozen=True)
class AuthContext:
    team_id: int
    team_name: str
    key_id: int
    role: str


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return f"cs_live_{secrets.token_urlsafe(24)}"


def issue_api_key(
    team_id: int,
    label: str = "default",
    role: str = "editor",
    created_by_email: Optional[str] = None,
) -> tuple[int, str]:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role}")

    raw_key = generate_api_key()
    key_hash = _hash_key(raw_key)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO api_keys (team_id, label, role, created_by_email, key_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (team_id, label, role, created_by_email, key_hash),
        )
    return int(cur.lastrowid), raw_key


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> AuthContext:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    key_hash = _hash_key(x_api_key)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT k.id AS key_id,
                   t.id AS team_id,
                   t.name AS team_name,
                   COALESCE(k.role, 'editor') AS role
            FROM api_keys k
            JOIN teams t ON t.id = k.team_id
            WHERE k.key_hash = ? AND k.revoked_at IS NULL
            """,
            (key_hash,),
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    return AuthContext(
        team_id=int(row["team_id"]),
        team_name=str(row["team_name"]),
        key_id=int(row["key_id"]),
        role=str(row["role"]),
    )


def require_roles(*allowed_roles: str):
    allowed = set(allowed_roles)

    def _checker(auth: AuthContext = Depends(require_api_key)) -> AuthContext:
        if auth.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{auth.role}' is not allowed",
            )
        return auth

    return _checker
