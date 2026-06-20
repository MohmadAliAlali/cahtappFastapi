import logging
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.api.v1 import auth, users, conversations, upload
from app.core.config import settings
from app.core.redis_client import redis_lifespan, get_redis
from app.db.base import Base
from app.db.session import engine, async_session_factory
from app.services.socketio_handler import sio

logger = logging.getLogger("chat")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.is_sqlite:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    async with redis_lifespan():
        yield


app = FastAPI(title="Chat App API", version="0.1.0", lifespan=lifespan)

# CORS - restricted for production, open for development
cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")
if cors_origins == ["*"] and not settings.is_sqlite:
    logger.warning("CORS_ORIGINS not set, allowing all origins (development only)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(conversations.router, prefix="/api/v1")
app.include_router(upload.router)

os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")

from socketio import ASGIApp
asgi_app = ASGIApp(sio, other_asgi_app=app)


@app.get("/health")
async def health():
    db_ok = False
    redis_ok = None

    try:
        async with async_session_factory() as db:
            await db.execute(text("SELECT 1"))
            db_ok = True
    except Exception as e:
        logger.warning("Health check DB failed: %s", e)

    try:
        redis = get_redis()
        if redis is not None:
            await redis.ping()
            redis_ok = True
        else:
            redis_ok = None
    except Exception as e:
        logger.warning("Health check Redis failed: %s", e)
        redis_ok = False

    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "database": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "down" if redis_ok is False else "disabled",
    }
