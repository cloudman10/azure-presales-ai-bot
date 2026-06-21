"""
app/routers/diagram.py

Phase 1: diagram render endpoints.  No AI — hardcoded sample only.
"""

import asyncio
import logging
import shutil

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from app.services.diagram_renderer import SAMPLE_ARCHITECTURE, render_architecture

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def diagram_health():
    """Check whether graphviz is available in the container."""
    dot_path = shutil.which("dot")
    if not dot_path:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "detail": "graphviz (dot) not found in PATH",
                "hint": "Container may still be starting; graphviz is installed in startup.sh",
            },
        )
    import subprocess
    result = subprocess.run(["dot", "-V"], capture_output=True, text=True)
    version = (result.stderr or result.stdout).strip()
    return {"status": "ok", "dot": dot_path, "version": version}


@router.get("/sample")
async def diagram_sample():
    """Render the hardcoded 3-tier sample and return it as image/png."""
    try:
        png_bytes: bytes = await asyncio.to_thread(render_architecture, SAMPLE_ARCHITECTURE)
    except FileNotFoundError as exc:
        logger.error("graphviz not found: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "error": "graphviz not available",
                "detail": str(exc),
                "hint": "GET /api/diagram/health for graphviz status",
            },
        )
    except Exception as exc:
        logger.exception("diagram render failed")
        return JSONResponse(
            status_code=500,
            content={"error": "render failed", "detail": str(exc)},
        )

    return Response(content=png_bytes, media_type="image/png")
