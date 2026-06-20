from datetime import datetime
from pydantic import BaseModel


class ConversationCreateRequest(BaseModel):
    user_id: str


class ConversationResponse(BaseModel):
    id: str
    type: str = "direct"
    created_at: datetime
    name: str | None = None
    avatar_url: str | None = None
    participant_ids: list[str] = []
    last_message: str | None = None
    last_message_at: datetime | None = None
    unread_count: int = 0

    model_config = {"from_attributes": True}


class ReplyPreview(BaseModel):
    id: str
    sender_name: str | None = None
    body: str | None = None
    file_url: str | None = None
    file_name: str | None = None
    file_type: str | None = None

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    sender_id: str
    sender_name: str | None = None
    sender_avatar_url: str | None = None
    encrypted_body: str
    iv: str
    body: str | None = None
    created_at: datetime
    edited_at: datetime | None = None
    deleted: bool = False
    forwarded: bool = False
    file_url: str | None = None
    file_name: str | None = None
    file_type: str | None = None
    file_size: int | None = None
    reply_to_id: str | None = None
    reply_to: ReplyPreview | None = None

    model_config = {"from_attributes": True}


class MessageListResponse(BaseModel):
    messages: list[MessageResponse]
    next_cursor: str | None = None


class SendMessageRequest(BaseModel):
    encrypted_body: str
    iv: str


class MarkReadRequest(BaseModel):
    last_read_msg_id: str


class ForwardMessageRequest(BaseModel):
    message_id: str
    target_conversation_id: str
