import io
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.agents import report_agent
from app.models.schemas import BasketAddRequest, BasketItem, BasketReportRequest
from app.state import sessions

router = APIRouter()

_SKU_RE = re.compile(r'VM:\s*(Standard_\S+|\S+)', re.IGNORECASE)
_REGION_RE = re.compile(r'Region:\s*(\S+)', re.IGNORECASE)
_OS_RE = re.compile(r'OS:\s*(\S+)', re.IGNORECASE)


def _auto_label(pricing_text: str) -> str:
    sku = m.group(1) if (m := _SKU_RE.search(pricing_text)) else None
    os_ = m.group(1) if (m := _OS_RE.search(pricing_text)) else None
    region = m.group(1) if (m := _REGION_RE.search(pricing_text)) else None
    parts = [p for p in (sku, os_, region) if p]
    return " · ".join(parts) if parts else "VM Quote"


def _basket(session_id: str) -> list[dict]:
    key = f"{session_id}_basket"
    if key not in sessions:
        sessions[key] = []
    return sessions[key]


@router.post("", response_model=list[BasketItem])
async def basket_add(request: BasketAddRequest) -> list[BasketItem]:
    label = request.label or _auto_label(request.pricing_text)
    item = {
        "id": str(uuid.uuid4()),
        "added_at": datetime.now(timezone.utc).isoformat(),
        "pricing_text": request.pricing_text,
        "label": label,
    }
    _basket(request.session_id).append(item)
    return _basket(request.session_id)


@router.get("", response_model=list[BasketItem])
async def basket_get(session_id: str = Query(...)) -> list[BasketItem]:
    return _basket(session_id)


@router.delete("/{item_id}")
async def basket_remove(item_id: str, session_id: str = Query(...)) -> dict:
    b = _basket(session_id)
    before = len(b)
    sessions[f"{session_id}_basket"] = [i for i in b if i["id"] != item_id]
    after = len(sessions[f"{session_id}_basket"])
    if before == after:
        raise HTTPException(status_code=404, detail="Item not found in basket")
    return {"removed": 1, "count": after}


@router.delete("")
async def basket_clear(session_id: str = Query(...)) -> dict:
    sessions[f"{session_id}_basket"] = []
    return {"count": 0}


@router.post("/report/excel")
async def basket_report_excel(request: BasketReportRequest) -> StreamingResponse:
    b = _basket(request.session_id)
    if not b:
        raise HTTPException(status_code=400, detail="Basket is empty")
    combined = "\n\n---\n\n".join(item["pricing_text"] for item in b)
    data = report_agent.generate_excel(combined, request.session_id)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="azure-vm-quote-basket.xlsx"'},
    )


@router.post("/report/pdf")
async def basket_report_pdf(request: BasketReportRequest) -> StreamingResponse:
    b = _basket(request.session_id)
    if not b:
        raise HTTPException(status_code=400, detail="Basket is empty")
    combined = "\n\n---\n\n".join(item["pricing_text"] for item in b)
    data = report_agent.generate_pdf(combined, request.session_id)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="azure-vm-quote-basket.pdf"'},
    )
