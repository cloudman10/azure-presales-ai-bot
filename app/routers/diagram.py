"""
app/routers/diagram.py

/api/diagram/* endpoints:
  GET  /health   — graphviz availability check
  GET  /sample   — render hardcoded 3-tier sample → image/png
  POST /chat     — AI architecture discovery conversation
  POST /render   — render architecture JSON → image/png
"""

import asyncio
import base64
import logging
import shutil
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.services import diagram_architect
from app.services.diagram_renderer import SAMPLE_ARCHITECTURE, render_architecture
from app.state import sessions

logger = logging.getLogger(__name__)

router = APIRouter()


class DiagramChatRequest(BaseModel):
    session_id: str
    message: str


class RenderRequest(BaseModel):
    title: str
    region: str
    resources: list[dict[str, Any]]
    connections: list[dict[str, Any]]


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


@router.post("/chat")
async def diagram_chat(body: DiagramChatRequest):
    """
    AI architecture discovery — one turn per call.

    Maintains conversation history in the session store under
    sessions["{session_id}_diagram_history"].

    Returns:
      {"type": "question", "reply": "<question>"}        — still gathering
      {"type": "architecture", "json": {...}}            — complete architecture
    """
    hist_key = f"{body.session_id}_diagram_history"
    if hist_key not in sessions:
        sessions[hist_key] = []
    history: list[dict] = sessions[hist_key]

    try:
        result = await diagram_architect.chat(history, body.message)
    except Exception as exc:
        logger.exception("diagram_chat failed: session=%s", body.session_id)
        return JSONResponse(
            status_code=500,
            content={"error": "chat failed", "detail": str(exc)},
        )

    if result.get("type") == "architecture":
        try:
            from app.services.diagram_renderer_svg import render_architecture_svg
            svg_bytes = await asyncio.to_thread(render_architecture_svg, result["json"])
            result["svg_b64"] = base64.b64encode(svg_bytes).decode()
        except Exception as exc:
            import traceback
            svg_tb = traceback.format_exc()
            logger.warning("SVG render failed, trying PNG fallback: %s\n%s", exc, svg_tb)
            result["svg_error"] = f"{type(exc).__name__}: {exc}"
            try:
                png_bytes = await asyncio.to_thread(render_architecture, result["json"])
                result["png_base64"] = base64.b64encode(png_bytes).decode()
            except Exception as exc2:
                logger.warning("PNG render also failed: %s", exc2)
                result["render_error"] = str(exc2)

    return result


@router.get("/svg-test")
async def diagram_svg_test():
    """Smoke-test the SVG renderer with a minimal zones payload. Returns JSON."""
    import traceback
    test_arch = {
        "title": "SVG Smoke Test",
        "zones": [
            {"id": "onprem", "type": "onprem", "label": "On-Premises",
             "resources": [{"id": "hv1", "type": "HyperVHost", "name": "HyperV Host"}]},
            {"id": "hub", "type": "hub", "label": "Hub VNet",
             "resources": [{"id": "fw1", "type": "AzureFirewall", "name": "Azure Firewall"}]},
        ],
        "connections": [{"from": "hv1", "to": "fw1", "label": "VPN"}],
    }
    try:
        from app.services.diagram_renderer_svg import render_architecture_svg
        svg_bytes = await asyncio.to_thread(render_architecture_svg, test_arch)
        return {"status": "ok", "bytes": len(svg_bytes), "preview": svg_bytes[:200].decode("utf-8", errors="replace")}
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}


@router.post("/render-drawio")
async def diagram_render_drawio(request: Request):
    """Convert architecture JSON to draw.io mxGraphModel XML. Returns {drawio_xml}."""
    try:
        arch = await request.json()
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": "invalid JSON", "detail": str(exc)})
    try:
        from app.services.diagram_renderer_drawio import render_drawio
        xml_str = await asyncio.to_thread(render_drawio, arch)
        return {"drawio_xml": xml_str}
    except Exception as exc:
        import traceback as _tb
        logger.exception("render-drawio failed")
        return JSONResponse(
            status_code=500,
            content={"error": "render failed", "detail": str(exc),
                     "traceback": _tb.format_exc()},
        )


@router.post("/render")
async def diagram_render(body: RenderRequest):
    """Render an architecture JSON dict to PNG. Returns image/png."""
    arch = body.model_dump()
    try:
        png_bytes = await asyncio.to_thread(render_architecture, arch)
    except FileNotFoundError as exc:
        logger.error("graphviz not found: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"error": "graphviz not available", "detail": str(exc)},
        )
    except Exception as exc:
        logger.exception("diagram render failed")
        return JSONResponse(
            status_code=500,
            content={"error": "render failed", "detail": str(exc)},
        )
    return Response(content=png_bytes, media_type="image/png")
