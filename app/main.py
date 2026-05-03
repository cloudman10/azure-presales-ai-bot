import os

from dotenv import load_dotenv

load_dotenv()

# Application Insights — must be configured before other imports to instrument all libraries
_ai_connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
if _ai_connection_string:
    from azure.monitor.opentelemetry import configure_azure_monitor
    configure_azure_monitor(connection_string=_ai_connection_string)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routers import chat

app = FastAPI(title="Azure VM Pricing Bot", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(chat.router, prefix="/api")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}
