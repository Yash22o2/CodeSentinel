"""Health check endpoint."""
from fastapi import APIRouter
from app.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health", summary="Health check")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "env": settings.app_env,
        "model": settings.groq_model,
    }
