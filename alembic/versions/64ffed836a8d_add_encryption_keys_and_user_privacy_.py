"""add encryption_keys and user_privacy tables

Revision ID: 64ffed836a8d
Revises:
Create Date: 2026-06-15 13:34:20.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "64ffed836a8d"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "encryption_keys",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=False),
            sa.ForeignKey("conversations.id"),
            unique=True,
            index=True,
        ),
        sa.Column("encrypted_key", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "user_privacy",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id"),
            unique=True,
            index=True,
        ),
        sa.Column("read_receipts", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("online_status", sa.Boolean, nullable=False, server_default="true"),
        sa.Column(
            "who_can_add_to_groups",
            sa.Enum("everyone", "contacts", "nobody", name="whocanadd"),
            nullable=False,
            server_default="everyone",
        ),
    )


def downgrade() -> None:
    op.drop_table("user_privacy")
    op.drop_table("encryption_keys")
    op.execute("DROP TYPE IF EXISTS whocanadd")
