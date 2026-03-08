from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routers.health import router as health_router
from app.routers.notifications import router as notifications_router
from app.routers.reminders import router as reminders_router
from app.routers.subscriptions import router as subscriptions_router
from app.routers.teams import router as teams_router

app = FastAPI(title="CancelShield API", version="0.3.0")

WEB_DIR = Path(__file__).resolve().parent / "web"


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/console")


app.mount("/console", StaticFiles(directory=WEB_DIR, html=True), name="console")
app.include_router(health_router)
app.include_router(teams_router)
app.include_router(subscriptions_router)
app.include_router(reminders_router)
app.include_router(notifications_router)
