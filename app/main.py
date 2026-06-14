import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv()

# Application Insights — must be configured before other imports to instrument all libraries
_ai_connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
if _ai_connection_string:
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(connection_string=_ai_connection_string)
    except Exception as _ai_exc:
        logging.getLogger(__name__).warning("configure_azure_monitor failed, telemetry disabled: %s", _ai_exc)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routers import basket, chat

app = FastAPI(title="Azure VM Pricing Bot", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")

app.include_router(chat.router, prefix="/api")
app.include_router(basket.router, prefix="/api/basket")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(
        str(_BASE_DIR / "static" / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}
