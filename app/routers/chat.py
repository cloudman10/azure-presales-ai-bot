from fastapi import APIRouter

from app.agents import orchestrator
from app.models.schemas import ChatRequest, ChatResponse, WelcomeResponse

router = APIRouter()

# Shared in-memory session store — cleared on restart
sessions: dict = {}

WELCOME_TEXT = (
    "Hello! I can look up Azure VM pricing for you.\n\n"
    "Try:\n"
    "  D4s_v5 Windows Australia East\n"
    "  5x E8s_v3 Linux Southeast Asia with 3-year RI\n"
    "  E8-4ads_v7 Windows Australia East\n"
    "  D2_v3 Windows Australia Southeast\n\n"
    "I will ask for anything that is missing."
)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    result = await orchestrator.run(request.session_id, request.message, sessions)
    return ChatResponse(
        reply=result["reply"],
        type=result["type"],
        session_id=request.session_id,
    )


@router.get("/welcome", response_model=WelcomeResponse)
async def welcome() -> WelcomeResponse:
    return WelcomeResponse(reply=WELCOME_TEXT)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}
