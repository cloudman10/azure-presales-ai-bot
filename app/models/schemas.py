from typing import Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    type: str  # "conversation" or "pricing"
    session_id: str
    picks: Optional[dict] = None  # set when type=="advisor" so frontend can fetch full pricing


class WelcomeResponse(BaseModel):
    reply: str


class ReportRequest(BaseModel):
    session_id: str
    pricing_text: str


class BasketAddRequest(BaseModel):
    session_id: str
    pricing_text: str
    label: Optional[str] = None  # human-readable name; auto-derived from pricing_text if omitted


class BasketItem(BaseModel):
    id: str
    added_at: str          # ISO 8601 UTC
    pricing_text: str
    label: Optional[str] = None


class BasketReportRequest(BaseModel):
    session_id: str
