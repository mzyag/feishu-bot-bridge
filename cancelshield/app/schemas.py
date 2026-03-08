from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class TeamCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    owner_email: str = Field(min_length=3, max_length=254)


class TeamBootstrapOut(BaseModel):
    team_id: int
    team_name: str
    api_key: str
    api_key_role: str


class TeamOut(BaseModel):
    team_id: int
    team_name: str
    role: str


class TeamMemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(pattern="^(admin|editor|viewer)$")


class TeamMemberOut(BaseModel):
    id: int
    team_id: int
    email: str
    role: str


class ApiKeyCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    role: str = Field(pattern="^(admin|editor|viewer)$")
    created_by_email: Optional[str] = Field(default=None, min_length=3, max_length=254)


class ApiKeyOut(BaseModel):
    key_id: int
    label: str
    role: str
    api_key: str


class NotificationChannelCreate(BaseModel):
    provider: str = Field(pattern="^(slack|feishu|generic)$")
    webhook_url: str = Field(min_length=10, max_length=500)
    enabled: bool = True


class NotificationChannelOut(BaseModel):
    id: int
    provider: str
    webhook_url: str
    enabled: bool


class NotificationTestOut(BaseModel):
    attempted: int
    sent: int
    failed: int


class SubscriptionCreate(BaseModel):
    vendor: str = Field(min_length=1, max_length=100)
    plan_name: Optional[str] = Field(default=None, max_length=100)
    amount: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    renewal_date: date
    owner_email: str = Field(min_length=3, max_length=254)
    notes: Optional[str] = Field(default=None, max_length=500)


class SubscriptionOut(BaseModel):
    id: int
    team_id: Optional[int]
    team_name: str
    vendor: str
    plan_name: Optional[str]
    amount: float
    currency: str
    renewal_date: date
    owner_email: str
    status: str
    notes: Optional[str]


class EvidenceCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=50)
    actor: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    page_url: Optional[str] = Field(default=None, max_length=500)
    screenshot_path: Optional[str] = Field(default=None, max_length=500)
    details: Optional[str] = Field(default=None, max_length=2000)


class EvidenceUploadCreate(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    event_type: str = Field(default="cancel_attempt", min_length=1, max_length=50)
    details: Optional[str] = Field(default=None, max_length=2000)
    page_url: Optional[str] = Field(default=None, max_length=500)
    occurred_at: Optional[datetime] = None
    file_name: str = Field(min_length=1, max_length=200)
    file_content_base64: str = Field(min_length=8)


class EvidenceOut(BaseModel):
    id: int
    subscription_id: int
    event_type: str
    actor: str
    occurred_at: datetime
    page_url: Optional[str]
    screenshot_path: Optional[str]
    details: Optional[str]


class ReminderPreview(BaseModel):
    subscription_id: int
    reminder_dates: list[date]


class ReminderRunOut(BaseModel):
    team_id: int
    triggered_on: date
    queued_count: int
    subscriptions: list[int]


class DisputeExportOut(BaseModel):
    subscription_id: int
    export_path: str
    evidence_count: int
