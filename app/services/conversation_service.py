import hashlib
import logging
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select, func, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Conversation, ConversationParticipant, Message
from app.models.user import User
from app.schemas.conversation import (
    ConversationResponse,
    MessageResponse,
    MessageListResponse,
    ReplyPreview,
)
from app.services.encryption_service import (
    get_or_create_conversation_key,
    encrypt_message,
    decrypt_message,
)

logger = logging.getLogger("chat.conversation")


async def create_direct_conversation(
    db: AsyncSession, current_user_id: str, other_user_id: str,
) -> ConversationResponse:
    if current_user_id == other_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create conversation with yourself",
        )

    other = await db.execute(select(User).where(User.id == other_user_id))
    if not other.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Check existing direct conversation with row lock
    subq = (
        select(ConversationParticipant.conversation_id)
        .where(ConversationParticipant.user_id == current_user_id)
        .cte()
    )
    existing = await db.execute(
        select(ConversationParticipant.conversation_id)
        .where(
            ConversationParticipant.conversation_id.in_(select(subq.c.conversation_id)),
            ConversationParticipant.user_id == other_user_id,
        )
        .with_for_update()
    )
    existing_row = existing.scalar_one_or_none()
    if existing_row:
        conv = await db.execute(select(Conversation).where(Conversation.id == existing_row))
        return await _build_conversation_response(db, conv.scalar_one(), current_user_id)

    conv = Conversation()
    db.add(conv)
    await db.flush()

    for uid in [current_user_id, other_user_id]:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=uid))

    await get_or_create_conversation_key(db, conv.id)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing2 = await db.execute(
            select(ConversationParticipant.conversation_id)
            .where(
                ConversationParticipant.user_id.in_([current_user_id, other_user_id]),
            )
            .group_by(ConversationParticipant.conversation_id)
            .having(func.count() == 2)
        )
        for row in existing2.all():
            conv_check = await db.execute(
                select(Conversation).where(Conversation.id == row[0])
            )
            existing_conv = conv_check.scalar_one_or_none()
            if existing_conv:
                return await _build_conversation_response(db, existing_conv, current_user_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conversation already exists",
        )

    logger.info("Created direct conversation %s between %s and %s", conv.id, current_user_id, other_user_id)
    return await _build_conversation_response(db, conv, current_user_id)


async def list_conversations(
    db: AsyncSession, user_id: str
) -> list[ConversationResponse]:
    part_cp = ConversationParticipant.__table__.alias("cp")
    last_read_msg = Message.__table__.alias("lrm")

    part_subq = (
        select(ConversationParticipant.conversation_id)
        .where(ConversationParticipant.user_id == user_id)
        .subquery()
    )

    # Unread count per conversation
    unread_subq = (
        select(
            ConversationParticipant.conversation_id,
            func.count(Message.id).label("unread"),
        )
        .select_from(ConversationParticipant)
        .outerjoin(
            Message,
            (Message.conversation_id == ConversationParticipant.conversation_id)
            & (Message.deleted == False)
            & (Message.sender_id != user_id),
        )
        .outerjoin(
            last_read_msg,
            last_read_msg.c.id == ConversationParticipant.last_read_msg_id,
        )
        .where(
            ConversationParticipant.user_id == user_id,
            (ConversationParticipant.last_read_msg_id == None)
            | (Message.created_at > last_read_msg.c.created_at),
        )
        .group_by(ConversationParticipant.conversation_id)
        .subquery()
    )

    last_msg_subq = (
        select(
            Message.conversation_id,
            Message.encrypted_body,
            Message.iv,
            Message.created_at,
            func.row_number()
            .over(partition_by=Message.conversation_id, order_by=desc(Message.created_at))
            .label("rn"),
        )
        .where(Message.deleted == False)
        .subquery()
    )

    query = (
        select(
            Conversation,
            last_msg_subq.c.encrypted_body,
            last_msg_subq.c.iv,
            last_msg_subq.c.created_at,
            func.coalesce(unread_subq.c.unread, 0),
        )
        .select_from(Conversation)
        .join(part_subq, Conversation.id == part_subq.c.conversation_id)
        .outerjoin(
            last_msg_subq,
            (Conversation.id == last_msg_subq.c.conversation_id)
            & (last_msg_subq.c.rn == 1),
        )
        .outerjoin(unread_subq, Conversation.id == unread_subq.c.conversation_id)
        .order_by(desc(last_msg_subq.c.created_at))
    )

    result = await db.execute(query)
    rows = result.all()

    # Load participants for direct chats
    conv_ids = [row[0].id for row in rows]
    participants_by_conv = {}
    if conv_ids:
        part_result = await db.execute(
            select(ConversationParticipant).where(
                ConversationParticipant.conversation_id.in_(conv_ids),
                ConversationParticipant.user_id != user_id,
            )
        )
        for p in part_result.scalars().all():
            participants_by_conv.setdefault(p.conversation_id, []).append(p.user_id)

    # Load user names for participants
    all_user_ids = set()
    for p_ids in participants_by_conv.values():
        all_user_ids.update(p_ids)
    user_names = {}
    if all_user_ids:
        user_result = await db.execute(
            select(User).where(User.id.in_(all_user_ids))
        )
        for u in user_result.scalars().all():
            user_names[u.id] = (u.display_name, u.avatar_url)

    responses = []
    for row in rows:
        conv = row[0]
        last_msg_body = row[1]
        last_msg_iv = row[2]
        unread_count = row[4]

        decrypted_last = None
        if last_msg_body and last_msg_iv:
            try:
                key = await get_or_create_conversation_key(db, conv.id)
                decrypted_last = decrypt_message(last_msg_body, last_msg_iv, key)
            except Exception:
                decrypted_last = "[encrypted]"

        name = None
        avatar_url = None
        participant_ids = []

        p_ids = participants_by_conv.get(conv.id, [])
        participant_ids = p_ids
        if p_ids:
            other_id = p_ids[0]
            uname, uavatar = user_names.get(other_id, (None, None))
            name = uname or "User"
            avatar_url = uavatar

        responses.append(
            ConversationResponse(
                id=conv.id,
                type="direct",
                created_at=conv.created_at,
                name=name,
                avatar_url=avatar_url,
                participant_ids=participant_ids,
                last_message=decrypted_last,
                last_message_at=row[3],
                unread_count=unread_count,
            )
        )
    return responses


async def _build_messages(
    db: AsyncSession, rows: list[Message], conv_key: bytes,
) -> list[MessageResponse]:
    sender_ids = {m.sender_id for m in rows}
    sender_map = {}
    if sender_ids:
        sender_result = await db.execute(select(User).where(User.id.in_(sender_ids)))
        for u in sender_result.scalars().all():
            sender_map[u.id] = (u.display_name, u.avatar_url)

    reply_to_ids = {m.reply_to_id for m in rows if m.reply_to_id}
    reply_map = {}
    if reply_to_ids:
        reply_result = await db.execute(select(Message).where(Message.id.in_(reply_to_ids)))
        for r in reply_result.scalars().all():
            reply_body = None
            try:
                reply_body = decrypt_message(r.encrypted_body, r.iv, conv_key)
            except Exception:
                reply_body = "[encrypted]"
            reply_sender = sender_map.get(r.sender_id)
            reply_map[r.id] = ReplyPreview(
                id=r.id,
                sender_name=reply_sender[0] if reply_sender else None,
                body=reply_body,
                file_url=r.file_url,
                file_name=r.file_name,
                file_type=r.file_type,
            )

    result = []
    for m in rows:
        body = None
        try:
            body = decrypt_message(m.encrypted_body, m.iv, conv_key)
        except Exception:
            body = "[encrypted]"
        sname, savatar = sender_map.get(m.sender_id, (None, None))
        result.append(MessageResponse(
            id=m.id,
            conversation_id=m.conversation_id,
            sender_id=m.sender_id,
            sender_name=sname,
            sender_avatar_url=savatar,
            encrypted_body=m.encrypted_body,
            iv=m.iv,
            body=body,
            created_at=m.created_at,
            edited_at=m.edited_at,
            deleted=m.deleted,
            forwarded=m.forwarded,
            file_url=m.file_url,
            file_name=m.file_name,
            file_type=m.file_type,
            file_size=m.file_size,
            reply_to_id=m.reply_to_id,
            reply_to=reply_map.get(m.reply_to_id) if m.reply_to_id else None,
        ))
    return result


async def get_messages(
    db: AsyncSession, conversation_id: str, user_id: str, cursor: str | None = None, limit: int = 50
) -> MessageListResponse:
    await _check_membership(db, conversation_id, user_id)

    conv_key = await get_or_create_conversation_key(db, conversation_id)

    query = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.deleted == False,
    )
    if cursor:
        try:
            parts = cursor.split("_", 1)
            cursor_ts_str = parts[0]
            cursor_id = parts[1] if len(parts) > 1 else ""
            cursor_ts = datetime.fromisoformat(cursor_ts_str)
            query = query.where(
                (Message.created_at < cursor_ts) |
                ((Message.created_at == cursor_ts) & (Message.id < cursor_id))
            )
        except (ValueError, IndexError):
            pass

    query = query.order_by(desc(Message.created_at), desc(Message.id)).limit(limit + 1)
    result = await db.execute(query)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    messages = await _build_messages(db, rows, conv_key)

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{last.created_at.isoformat()}_{last.id}"

    return MessageListResponse(
        messages=messages,
        next_cursor=next_cursor,
    )


async def mark_read(
    db: AsyncSession, conversation_id: str, user_id: str, last_read_msg_id: str
) -> dict:
    await _check_membership(db, conversation_id, user_id)

    msg_result = await db.execute(
        select(Message.id).where(
            Message.id == last_read_msg_id,
            Message.conversation_id == conversation_id,
        )
    )
    if not msg_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message does not belong to this conversation",
        )

    result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        )
    )
    participant = result.scalar_one_or_none()
    if participant:
        participant.last_read_msg_id = last_read_msg_id
        await db.commit()

    return {"message": "Read status updated"}


async def save_message(
    db: AsyncSession, conversation_id: str, sender_id: str, body: str,
    file_url: str | None = None, file_name: str | None = None,
    file_type: str | None = None, file_size: int | None = None,
    reply_to_id: str | None = None,
) -> MessageResponse:
    await _check_membership(db, conversation_id, sender_id)

    conv_key = await get_or_create_conversation_key(db, conversation_id)
    encrypted_body, iv = encrypt_message(body, conv_key)

    reply_preview = None
    valid_reply_to_id = None
    if reply_to_id:
        reply_result = await db.execute(
            select(Message).where(Message.id == reply_to_id, Message.deleted == False)
        )
        reply_msg = reply_result.scalar_one_or_none()
        if reply_msg:
            valid_reply_to_id = reply_to_id
            reply_body = None
            try:
                reply_body = decrypt_message(reply_msg.encrypted_body, reply_msg.iv, conv_key)
            except Exception:
                reply_body = "[encrypted]"
            reply_user = await db.execute(select(User).where(User.id == reply_msg.sender_id))
            ru = reply_user.scalar_one_or_none()
            reply_preview = ReplyPreview(
                id=reply_msg.id,
                sender_name=ru.display_name if ru else None,
                body=reply_body,
                file_url=reply_msg.file_url,
                file_name=reply_msg.file_name,
                file_type=reply_msg.file_type,
            )

    msg = Message(
        conversation_id=conversation_id,
        sender_id=sender_id,
        encrypted_body=encrypted_body,
        iv=iv,
        file_url=file_url,
        file_name=file_name,
        file_type=file_type,
        file_size=file_size,
        reply_to_id=valid_reply_to_id,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)

    user = await db.execute(select(User).where(User.id == sender_id))
    u = user.scalar_one_or_none()
    sname = u.display_name if u else None
    savatar = u.avatar_url if u else None

    return MessageResponse(
        id=msg.id,
        conversation_id=msg.conversation_id,
        sender_id=msg.sender_id,
        sender_name=sname,
        sender_avatar_url=savatar,
        encrypted_body=msg.encrypted_body,
        iv=msg.iv,
        body=body,
        created_at=msg.created_at,
        edited_at=msg.edited_at,
        deleted=msg.deleted,
        forwarded=msg.forwarded,
        file_url=msg.file_url,
        file_name=msg.file_name,
        file_type=msg.file_type,
        file_size=msg.file_size,
        reply_to_id=msg.reply_to_id,
        reply_to=reply_preview,
    )


async def forward_message(
    db: AsyncSession, message_id: str, source_conv_id: str,
    target_conv_id: str, user_id: str,
) -> MessageResponse:
    await _check_membership(db, source_conv_id, user_id)
    await _check_membership(db, target_conv_id, user_id)

    msg_result = await db.execute(
        select(Message).where(Message.id == message_id, Message.deleted == False)
    )
    msg = msg_result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    source_key = await get_or_create_conversation_key(db, source_conv_id)
    body = decrypt_message(msg.encrypted_body, msg.iv, source_key)

    target_key = await get_or_create_conversation_key(db, target_conv_id)
    encrypted_body, iv = encrypt_message(body, target_key)

    new_msg = Message(
        conversation_id=target_conv_id,
        sender_id=user_id,
        encrypted_body=encrypted_body,
        iv=iv,
        forwarded=True,
        file_url=msg.file_url,
        file_name=msg.file_name,
        file_type=msg.file_type,
        file_size=msg.file_size,
    )
    db.add(new_msg)
    await db.commit()
    await db.refresh(new_msg)

    user = await db.execute(select(User).where(User.id == user_id))
    u = user.scalar_one_or_none()
    sname = u.display_name if u else None
    savatar = u.avatar_url if u else None

    return MessageResponse(
        id=new_msg.id,
        conversation_id=new_msg.conversation_id,
        sender_id=new_msg.sender_id,
        sender_name=sname,
        sender_avatar_url=savatar,
        encrypted_body=new_msg.encrypted_body,
        iv=new_msg.iv,
        body=body,
        created_at=new_msg.created_at,
        edited_at=new_msg.edited_at,
        deleted=new_msg.deleted,
        forwarded=True,
        file_url=new_msg.file_url,
        file_name=new_msg.file_name,
        file_type=new_msg.file_type,
        file_size=new_msg.file_size,
    )


async def _check_membership(db: AsyncSession, conversation_id: str, user_id: str):
    result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == user_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this conversation",
        )


def _to_response(conv: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=conv.id,
        created_at=conv.created_at,
    )


async def _build_conversation_response(
    db: AsyncSession, conv: Conversation, user_id: str
) -> ConversationResponse:
    part_result = await db.execute(
        select(ConversationParticipant).where(
            ConversationParticipant.conversation_id == conv.id,
            ConversationParticipant.user_id != user_id,
        )
    )
    others = part_result.scalars().all()
    participant_ids = [p.user_id for p in others]
    if others:
        user_result = await db.execute(select(User).where(User.id == others[0].user_id))
        u = user_result.scalar_one_or_none()
        name = u.display_name if u else None
        avatar_url = u.avatar_url if u else None
    else:
        name = None
        avatar_url = None

    return ConversationResponse(
        id=conv.id,
        created_at=conv.created_at,
        name=name,
        avatar_url=avatar_url,
        participant_ids=participant_ids,
    )