import io
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.agents import report_agent
from app.models.schemas import (
    BasketAddRequest,
    BasketItem,
    BasketReportRequest,
    BasketTotalResponse,
)
from app.state import sessions

router = APIRouter()


def _basket(session_id: str) -> list[dict]:
    key = f"{session_id}_basket"
    if key not in sessions:
        sessions[key] = []
    return sessions[key]


def _line_total(vm_unit_cost: float, disks: list, count: int) -> float:
    disk_cost = sum(d.cost if hasattr(d, "cost") else d["cost"] for d in disks)
    return round((vm_unit_cost + disk_cost) * count, 4)


@router.post("", response_model=list[BasketItem])
async def basket_add(request: BasketAddRequest) -> list[BasketItem]:
    label = request.label or f"{request.sku} - {request.os} - {request.region}"
    item = {
        "id": str(uuid.uuid4()),
        "added_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "sku": request.sku,
        "os": request.os,
        "region": request.region,
        "term": request.term,
        "count": request.count,
        "vm_unit_cost": request.vm_unit_cost,
        "disks": [d.model_dump() for d in request.disks],
        "line_total": _line_total(request.vm_unit_cost, request.disks, request.count),
        "pricing_text": request.pricing_text,
    }
    _basket(request.session_id).append(item)
    return _basket(request.session_id)


@router.get("/total", response_model=BasketTotalResponse)
async def basket_total(session_id: str = Query(...)) -> BasketTotalResponse:
    b = _basket(session_id)
    return BasketTotalResponse(
        grand_total=round(sum(i["line_total"] for i in b), 4),
        item_count=len(b),
    )


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
    texts = [i["pricing_text"] for i in b if i.get("pricing_text")]
    if not texts:
        raise HTTPException(status_code=400, detail="No pricing_text available for report generation")
    combined = "\n\n---\n\n".join(texts)
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
    texts = [i["pricing_text"] for i in b if i.get("pricing_text")]
    if not texts:
        raise HTTPException(status_code=400, detail="No pricing_text available for report generation")
    combined = "\n\n---\n\n".join(texts)
    data = report_agent.generate_pdf(combined, request.session_id)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="azure-vm-quote-basket.pdf"'},
    )
