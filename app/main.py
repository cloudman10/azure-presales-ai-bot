from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routers import chat

app = FastAPI(title="Azure VM Pricing Bot", version="1.0.0")

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(chat.router, prefix="/api")


@app.get("/")
async def root() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}
