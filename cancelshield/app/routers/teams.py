from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.db import get_conn
from app.schemas import (
    ApiKeyCreate,
    ApiKeyOut,
    TeamBootstrapOut,
    TeamCreate,
    TeamMemberCreate,
    TeamMemberOut,
    TeamOut,
)
from app.security import AuthContext, issue_api_key, require_api_key, require_roles

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


@router.post("/bootstrap", response_model=TeamBootstrapOut, status_code=201)
def bootstrap_team(payload: TeamCreate) -> TeamBootstrapOut:
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM teams WHERE name = ?", (payload.name,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Team already exists")

        cur = conn.execute("INSERT INTO teams (name) VALUES (?)", (payload.name,))
        team_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO team_members (team_id, email, role)
            VALUES (?, ?, 'admin')
            """,
            (team_id, payload.owner_email),
        )

    _, raw_key = issue_api_key(
        team_id=team_id,
        label="bootstrap-admin",
        role="admin",
        created_by_email=payload.owner_email,
    )
    return TeamBootstrapOut(
        team_id=team_id,
        team_name=payload.name,
        api_key=raw_key,
        api_key_role="admin",
    )


@router.get("/me", response_model=TeamOut)
def get_current_team(auth: AuthContext = Depends(require_api_key)) -> TeamOut:
    return TeamOut(team_id=auth.team_id, team_name=auth.team_name, role=auth.role)


@router.get("/members", response_model=list[TeamMemberOut])
def list_members(auth: AuthContext = Depends(require_api_key)) -> list[TeamMemberOut]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, team_id, email, role
            FROM team_members
            WHERE team_id = ?
            ORDER BY id ASC
            """,
            (auth.team_id,),
        ).fetchall()
    return [TeamMemberOut(**dict(row)) for row in rows]


@router.post("/members", response_model=TeamMemberOut, status_code=201)
def add_member(
    payload: TeamMemberCreate,
    auth: AuthContext = Depends(require_roles("admin")),
) -> TeamMemberOut:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM team_members WHERE team_id = ? AND email = ?",
            (auth.team_id, payload.email),
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Member already exists")

        cur = conn.execute(
            """
            INSERT INTO team_members (team_id, email, role)
            VALUES (?, ?, ?)
            """,
            (auth.team_id, payload.email, payload.role),
        )
        row = conn.execute(
            "SELECT id, team_id, email, role FROM team_members WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()

    return TeamMemberOut(**dict(row))


@router.post("/api-keys", response_model=ApiKeyOut, status_code=201)
def create_api_key(
    payload: ApiKeyCreate,
    auth: AuthContext = Depends(require_roles("admin")),
) -> ApiKeyOut:
    key_id, raw_key = issue_api_key(
        team_id=auth.team_id,
        label=payload.label,
        role=payload.role,
        created_by_email=payload.created_by_email,
    )
    return ApiKeyOut(key_id=key_id, label=payload.label, role=payload.role, api_key=raw_key)
