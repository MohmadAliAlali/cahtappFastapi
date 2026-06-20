import uuid

from sqlalchemy import String, Boolean, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
import enum

from app.db.base import Base


class WhoCanAdd(str, enum.Enum):
    everyone = "everyone"
    contacts = "contacts"
    nobody = "nobody"


class UserPrivacy(Base):
    __tablename__ = "user_privacy"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), unique=True, index=True
    )
    read_receipts: Mapped[bool] = mapped_column(Boolean, default=True)
    online_status: Mapped[bool] = mapped_column(Boolean, default=True)
    who_can_add_to_groups: Mapped[WhoCanAdd] = mapped_column(
        SAEnum(WhoCanAdd), default=WhoCanAdd.everyone
    )
