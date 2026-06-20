import hashlib
import logging
import secrets
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from fastapi import HTTPException, status
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password, verify_password, create_access_token, create_refresh_token
from app.models.user import User, EmailVerificationCode, RefreshToken
from app.models.message import ConversationParticipant, Message
from app.schemas.auth import UserResponse, AuthResponse
from app.services.email_service import send_verification_code

logger = logging.getLogger("chat.auth")


def _generate_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


async def register(
    db: AsyncSession, email: str, password: str, display_name: str
) -> dict:
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user = User(
        email=email,
        hashed_password=hash_password(password),
        display_name=display_name,
    )
    db.add(user)
    await db.flush()

    code = _generate_code()
    verification = EmailVerificationCode(
        user_id=user.id,
        code=code,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(verification)
    await db.commit()

    await send_verification_code(email, code)

    return {"message": "Registration successful. Check your email for verification code."}


async def verify_email(db: AsyncSession, code: str) -> dict:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(EmailVerificationCode).where(
            EmailVerificationCode.code == code,
            EmailVerificationCode.expires_at > now,
        )
    )
    verification = result.scalar_one_or_none()
    if not verification:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code",
        )

    user_result = await db.execute(select(User).where(User.id == verification.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.is_active = True
    # Invalidate ALL verification codes for this user
    all_codes = await db.execute(
        select(EmailVerificationCode).where(
            EmailVerificationCode.user_id == user.id
        )
    )
    for code_record in all_codes.scalars().all():
        await db.delete(code_record)
    await db.commit()

    return {"message": "Email verified successfully"}


async def login(db: AsyncSession, email: str, password: str) -> AuthResponse:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified",
        )

    access_token = create_access_token(user.id)
    refresh_token_value = create_refresh_token(user.id)

    token_hash = hashlib.sha256(refresh_token_value.encode()).hexdigest()
    refresh_record = RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(refresh_record)

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token_value,
        user=UserResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
        ),
    )


async def refresh_tokens(db: AsyncSession, refresh_token_value: str) -> AuthResponse:
    try:
        payload = jwt.decode(
            refresh_token_value, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        user_id = payload.get("sub")
        jti = payload.get("jti")
        if not user_id or not jti:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    token_hash = hashlib.sha256(refresh_token_value.encode()).hexdigest()
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False,
        )
    )
    stored = result.scalar_one_or_none()
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token revoked or not found",
        )

    stored.revoked = True
    await db.flush()

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    new_access = create_access_token(user.id)
    new_refresh = create_refresh_token(user.id)

    new_token_hash = hashlib.sha256(new_refresh.encode()).hexdigest()
    new_record = RefreshToken(
        user_id=user.id,
        token_hash=new_token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(new_record)
    await db.commit()

    return AuthResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        user=UserResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
        ),
    )


async def logout(db: AsyncSession, refresh_token_value: str | None) -> dict:
    if refresh_token_value:
        token_hash = hashlib.sha256(refresh_token_value.encode()).hexdigest()
        result = await db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked == False,
            )
        )
        stored = result.scalar_one_or_none()
        if stored:
            stored.revoked = True
            await db.commit()
    return {"message": "Logged out successfully"}


async def delete_account(db: AsyncSession, user_id: str) -> dict:
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Remove all related data
    await db.execute(
        RefreshToken.__table__.delete().where(RefreshToken.user_id == user_id)
    )
    await db.execute(
        EmailVerificationCode.__table__.delete().where(EmailVerificationCode.user_id == user_id)
    )

    # Leave all conversations
    await db.execute(
        ConversationParticipant.__table__.delete().where(
            ConversationParticipant.user_id == user_id
        )
    )

    # Remove messages sent by user
    await db.execute(
        Message.__table__.update().where(Message.sender_id == user_id).values(deleted=True)
    )

    await db.delete(user)
    await db.commit()
    logger.info("Deleted account %s and all related data", user_id)
    return {"message": "Account deleted successfully"}


async def resend_code(db: AsyncSession, email: str) -> dict:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already verified",
        )

    code = _generate_code()
    verification = EmailVerificationCode(
        user_id=user.id,
        code=code,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db.add(verification)
    await db.commit()

    await send_verification_code(email, code)

    return {"message": "Verification code resent"}


async def change_password(
    db: AsyncSession, user_id: str, current_password: str, new_password: str
) -> dict:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if not verify_password(current_password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    if len(new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 8 characters",
        )

    user.hashed_password = hash_password(new_password)
    await db.commit()
    logger.info("Password changed for user %s", user_id)
    return {"message": "Password changed successfully"}
