import io

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.agents import orchestrator, report_agent
from app.models.schemas import ChatRequest, ChatResponse, ReportRequest, WelcomeResponse

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


@router.post("/report/excel")
async def report_excel(request: ReportRequest) -> StreamingResponse:
    data = report_agent.generate_excel(request.pricing_text, request.session_id)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="azure-vm-pricing.xlsx"'},
    )


@router.post("/report/pdf")
async def report_pdf(request: ReportRequest) -> StreamingResponse:
    data = report_agent.generate_pdf(request.pricing_text, request.session_id)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="azure-vm-pricing.pdf"'},
    )


@router.get("/welcome", response_model=WelcomeResponse)
async def welcome() -> WelcomeResponse:
    return WelcomeResponse(reply=WELCOME_TEXT)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}
