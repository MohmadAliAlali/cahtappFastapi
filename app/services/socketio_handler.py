import logging
import uuid
from datetime import datetime, timezone
import socketio
import urllib.parse
from jose import jwt, JWTError
from sqlalchemy import select

from app.core.config import settings
from app.core.redis_client import get_redis
from app.db.session import async_session_factory
from app.models.user import User
from app.services import conversation_service

logger = logging.getLogger("chat.socketio")

redis_client = get_redis()
if redis_client:
    from socketio import AsyncRedisManager
    client_manager = AsyncRedisManager(settings.REDIS_URL)
else:
    client_manager = None

sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    client_manager=client_manager,
)


def _extract_token(auth, environ) -> str | None:
    if auth and isinstance(auth, dict):
        token = auth.get("token")
        if token:
            return token
    try:
        qs = environ.get("QUERY_STRING", "")
        if qs:
            params = urllib.parse.parse_qs(qs)
            tokens = params.get("token", [])
            if tokens:
                return tokens[0]
    except Exception as e:
        logger.debug("Failed to parse QUERY_STRING: %s", e)
    try:
        scope = environ.get("asgi.scope", {})
        qs_bytes = scope.get("query_string", b"")
        if qs_bytes:
            params = urllib.parse.parse_qs(qs_bytes.decode("utf-8", errors="replace"))
            tokens = params.get("token", [])
            if tokens:
                return tokens[0]
    except Exception as e:
        logger.debug("Failed to parse asgi.scope.query_string: %s", e)
    return None


async def _get_user_from_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            return None
        async with async_session_factory() as db:
            result = await db.execute(
                select(User).where(User.id == user_id, User.is_active == True)
            )
            return result.scalar_one_or_none()
    except JWTError as e:
        logger.warning("JWT decode error: %s", e)
        return None


@sio.event
async def connect(sid, environ, auth):
    logger.info("CONNECT: sid=%s auth=%s", sid, auth)
    token = _extract_token(auth, environ)
    if not token:
        logger.warning("No token found in auth or query string")
        return False
    logger.info("Token validated")
    user = await _get_user_from_token(token)
    if not user:
        logger.warning("Invalid or inactive user token")
        return False

    await sio.save_session(sid, {"user_id": user.id})
    logger.info("User %s connected with sid %s", user.id, sid)
    return True


@sio.event
async def join(sid, data):
    conversation_id = data.get("conversation_id") if isinstance(data, dict) else None
    if not conversation_id:
        await sio.emit("error", {"message": "Invalid conversation_id"}, to=sid)
        return
    session = await sio.get_session(sid)
    user_id = session.get("user_id")
    is_member = False
    async with async_session_factory() as db:
        result = await db.execute(
            select(ConversationParticipant).where(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id == user_id,
            )
        )
        is_member = result.scalar_one_or_none() is not None
    if not is_member:
        await sio.emit("error", {"message": "Not a member of this conversation"}, to=sid)
        return
    await sio.enter_room(sid, conversation_id)
    await sio.emit("joined", {"conversation_id": conversation_id}, to=sid)


@sio.event
async def leave(sid, data):
    conversation_id = data.get("conversation_id") if isinstance(data, dict) else None
    if conversation_id:
        await sio.leave_room(sid, conversation_id)
    await sio.emit("left", {}, to=sid)


@sio.event
async def send_message(sid, data):
    if not isinstance(data, dict):
        return
    conversation_id = data.get("conversation_id")
    body = data.get("body", "")
    file_url = data.get("file_url")
    file_name = data.get("file_name")
    file_type = data.get("file_type")
    file_size = data.get("file_size")
    reply_to_id = data.get("reply_to_id")
    logger.info("send_message: conv=%s body_len=%d file=%s reply_to=%s",
        conversation_id, len(body) if body else 0, file_url or "none", reply_to_id or "none")
    if not conversation_id or (not body and not file_url and not reply_to_id):
        logger.warning("send_message: invalid message from sid=%s", sid)
        await sio.emit("error", {"message": "Invalid message"}, to=sid)
        return

    session = await sio.get_session(sid)
    user_id = session.get("user_id")
    if not user_id:
        logger.warning("send_message: unauthorized sid=%s", sid)
        await sio.emit("error", {"message": "Unauthorized"}, to=sid)
        return

    async with async_session_factory() as db:
        try:
            saved = await conversation_service.save_message(
                db, conversation_id, user_id, body,
                file_url=file_url, file_name=file_name,
                file_type=file_type, file_size=file_size,
                reply_to_id=reply_to_id,
            )
        except Exception as e:
            logger.error("send_message: save failed: %s", e, exc_info=True)
            await sio.emit("error", {"message": f"Save failed: {str(e)}"}, to=sid)
            return

    reply_to_data = None
    if saved.reply_to:
        reply_to_data = {
            "id": saved.reply_to.id,
            "sender_name": saved.reply_to.sender_name,
            "body": saved.reply_to.body,
            "file_url": saved.reply_to.file_url,
            "file_name": saved.reply_to.file_name,
            "file_type": saved.reply_to.file_type,
        }
        logger.info("send_message: reply_to_data id=%s name=%s body_len=%d",
            saved.reply_to.id, saved.reply_to.sender_name or "none",
            len(saved.reply_to.body or ""))

    payload = {
        "type": "new_message",
        "conversation_id": conversation_id,
        "message": {
            "id": saved.id,
            "conversation_id": saved.conversation_id,
            "sender_id": saved.sender_id,
            "sender_name": saved.sender_name,
            "sender_avatar_url": saved.sender_avatar_url,
            "body": saved.body,
            "encrypted_body": saved.encrypted_body,
            "iv": saved.iv,
            "created_at": saved.created_at.isoformat(),
            "edited_at": saved.edited_at.isoformat() if saved.edited_at else None,
            "file_url": saved.file_url,
            "file_name": saved.file_name,
            "file_type": saved.file_type,
            "file_size": saved.file_size,
            "reply_to_id": saved.reply_to_id,
            "reply_to": reply_to_data,
        },
    }
    await sio.emit("new_message", payload, room=conversation_id)


@sio.event
async def disconnect(sid):
    logger.info("User disconnected: %s", sid)