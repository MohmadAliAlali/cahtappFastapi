from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import rate_limit
from app.db.session import get_db
from app.schemas.auth import (
    RegisterRequest,
    VerifyEmailRequest,
    LoginRequest,
    RefreshRequest,
    ResendCodeRequest,
    AuthResponse,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register")
@rate_limit(limit=5, window_seconds=300, key_prefix="auth")
async def register(request: Request, body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    return await auth_service.register(db, body.email, body.password, body.display_name)


@router.post("/verify-email")
@rate_limit(limit=10, window_seconds=300, key_prefix="auth")
async def verify_email(request: Request, body: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    return await auth_service.verify_email(db, body.code)


@router.post("/login", response_model=AuthResponse)
@rate_limit(limit=10, window_seconds=300, key_prefix="auth")
async def login(request: Request, body: LoginRequest, db: AsyncSession = Depends(get_db)):
    return await auth_service.login(db, body.email, body.password)


@router.post("/refresh", response_model=AuthResponse)
@rate_limit(limit=20, window_seconds=300, key_prefix="auth")
async def refresh(request: Request, body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    return await auth_service.refresh_tokens(db, body.refresh_token)


@router.post("/logout")
async def logout(request: Request, body: RefreshRequest | None = None, db: AsyncSession = Depends(get_db)):
    token = body.refresh_token if body else None
    return await auth_service.logout(db, token)


@router.post("/resend-code")
@rate_limit(limit=3, window_seconds=300, key_prefix="auth")
async def resend_code(request: Request, body: ResendCodeRequest, db: AsyncSession = Depends(get_db)):
    return await auth_service.resend_code(db, body.email)
