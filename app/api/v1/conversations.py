from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db.session import get_db
from app.models.message import Message
from app.models.user import User
from app.schemas.conversation import (
    ConversationCreateRequest,
    ConversationResponse,
    MessageListResponse,
    MessageResponse,
    SendMessageRequest,
    MarkReadRequest,
    ForwardMessageRequest,
)
from app.services import conversation_service

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=ConversationResponse)
async def create_conversation(
    body: ConversationCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await conversation_service.create_direct_conversation(
        db, current_user.id, body.user_id
    )


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await conversation_service.list_conversations(db, current_user.id)


@router.get("/{conversation_id}/messages", response_model=MessageListResponse)
async def get_messages(
    conversation_id: str,
    cursor: str | None = Query(None),
    limit: int = Query(50, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await conversation_service.get_messages(db, conversation_id, current_user.id, cursor, limit)


@router.post("/{conversation_id}/read")
async def mark_read(
    conversation_id: str,
    body: MarkReadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await conversation_service.mark_read(
        db, conversation_id, current_user.id, body.last_read_msg_id
    )


@router.post("/forward", response_model=MessageResponse)
async def forward_message(
    body: ForwardMessageRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    source_msg = await db.execute(
        select(Message).where(Message.id == body.message_id, Message.deleted == False)
    )
    msg = source_msg.scalar_one_or_none()
    if not msg:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return await conversation_service.forward_message(
        db, body.message_id, msg.conversation_id,
        body.target_conversation_id, current_user.id,
    )