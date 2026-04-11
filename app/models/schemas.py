from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    type: str  # "conversation" or "pricing"
    session_id: str


class WelcomeResponse(BaseModel):
    reply: str


class ReportRequest(BaseModel):
    session_id: str
    pricing_text: str
