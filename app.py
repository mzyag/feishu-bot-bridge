import json
import os
from functools import lru_cache
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request

load_dotenv()

app = FastAPI(title="Feishu Bot Bridge")


class Settings:
    def __init__(self) -> None:
        self.feishu_app_id = os.getenv("FEISHU_APP_ID", "").strip()
        self.feishu_app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        self.feishu_verification_token = os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip()
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()

        allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
        self.allowed_user_ids = {x.strip() for x in allowed.split(",") if x.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


async def get_tenant_access_token(settings: Settings) -> str:
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise HTTPException(status_code=500, detail="Missing FEISHU_APP_ID / FEISHU_APP_SECRET")

    payload = {
        "app_id": settings.feishu_app_id,
        "app_secret": settings.feishu_app_secret,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json=payload,
        )
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 0:
        raise HTTPException(status_code=500, detail=f"Failed to get tenant token: {data}")
    return data["tenant_access_token"]


async def reply_text(open_id: str, text: str, settings: Settings) -> None:
    token = await get_tenant_access_token(settings)
    payload = {
        "receive_id": open_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            headers=headers,
            json=payload,
        )
    data = resp.json()
    if resp.status_code != 200 or data.get("code") != 0:
        raise HTTPException(status_code=500, detail=f"Failed to send message: {data}")


def extract_text(content_raw: str) -> str:
    if not content_raw:
        return ""
    try:
        obj = json.loads(content_raw)
        if isinstance(obj, dict):
            return str(obj.get("text", "")).strip()
    except Exception:
        pass
    return content_raw.strip()


async def generate_reply(user_text: str, settings: Settings) -> str:
    if not settings.openai_api_key:
        return f"已收到：{user_text}\n（当前未配置 OPENAI_API_KEY，先回显模式）"

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.openai_model,
        "input": [
            {
                "role": "system",
                "content": "You are a concise assistant in Feishu chat.",
            },
            {
                "role": "user",
                "content": user_text,
            },
        ],
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post("https://api.openai.com/v1/responses", headers=headers, json=payload)

    if resp.status_code != 200:
        return f"模型调用失败：HTTP {resp.status_code} {resp.text[:200]}"

    data = resp.json()
    return (data.get("output_text") or "").strip() or "收到，你可以继续发问题。"


@app.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "has_feishu_app_id": bool(settings.feishu_app_id),
        "has_verification_token": bool(settings.feishu_verification_token),
        "allowed_user_ids_count": len(settings.allowed_user_ids),
        "has_openai_key": bool(settings.openai_api_key),
    }


@app.post("/feishu/events")
async def feishu_events(req: Request) -> dict:
    settings = get_settings()
    body = await req.json()

    # URL verification handshake
    if body.get("type") == "url_verification":
        if settings.feishu_verification_token and body.get("token") != settings.feishu_verification_token:
            raise HTTPException(status_code=401, detail="Invalid verification token")
        return {"challenge": body.get("challenge")}

    # Event callback verification
    if settings.feishu_verification_token:
        token = body.get("header", {}).get("token") or body.get("token")
        if token != settings.feishu_verification_token:
            raise HTTPException(status_code=401, detail="Invalid verification token")

    event = body.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})
    sender_id = sender.get("sender_id", {}) if isinstance(sender, dict) else {}
    open_id: Optional[str] = sender_id.get("open_id")

    # Only handle text messages from users
    if message.get("message_type") != "text":
        return {"ok": True, "ignored": "non-text"}
    if not open_id:
        return {"ok": True, "ignored": "missing-open-id"}

    # User whitelist
    if settings.allowed_user_ids and open_id not in settings.allowed_user_ids:
        return {"ok": True, "ignored": "user-not-allowed"}

    user_text = extract_text(message.get("content", ""))
    if not user_text:
        return {"ok": True, "ignored": "empty-text"}

    reply = await generate_reply(user_text, settings)
    await reply_text(open_id, reply, settings)
    return {"ok": True}

