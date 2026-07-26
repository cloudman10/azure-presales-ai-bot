"""
Bulk VM pricing router — NOT wired into main.py yet.

Wire in by adding to app/main.py:
    from app.routers import bulk_pricing as bulk_pricing_router
    app.include_router(bulk_pricing_router.router, prefix="/api/bulk-pricing")

Endpoints (once wired):
    POST /api/bulk-pricing/upload    — process xlsx, return JSON breakdown
    POST /api/bulk-pricing/export    — process xlsx, return xlsx download
"""

import logging

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from io import BytesIO

from app.services.bulk_pricing import generate_bulk_excel, process_inventory

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB guard


@router.post("/upload")
async def upload_inventory(file: UploadFile = File(...)):
    """Process an inventory Excel file and return JSON pricing breakdown."""
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx / .xls files accepted")

    try:
        result = await process_inventory(raw)
    except Exception as exc:
        logger.exception("bulk_pricing upload failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return result


@router.post("/export")
async def export_inventory(file: UploadFile = File(...)):
    """Process an inventory Excel file and return an xlsx pricing report."""
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx / .xls files accepted")

    try:
        result = await process_inventory(raw)
        xlsx_bytes = generate_bulk_excel(result)
    except Exception as exc:
        logger.exception("bulk_pricing export failed")
        raise HTTPException(status_code=500, detail=str(exc))

    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="azure-vm-bulk-pricing.xlsx"'},
    )


@router.post("/export-result")
async def export_from_result(result: dict = Body(...)):
    """Generate Excel from an already-processed result dict (no re-pricing)."""
    try:
        xlsx_bytes = generate_bulk_excel(result)
    except Exception as exc:
        logger.exception("bulk_pricing export-result failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="azure-vm-bulk-pricing.xlsx"'},
    )
