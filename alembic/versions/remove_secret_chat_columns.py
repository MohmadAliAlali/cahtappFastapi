"""remove secret chat columns

Revision ID: remove_secret_chat_columns
Revises: 64ffed836a8d
Create Date: 2026-06-20 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "remove_secret_chat_columns"
down_revision: Union[str, None] = "64ffed836a8d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("conversations", "is_secret")
    op.drop_column("conversations", "expires_at")
    op.drop_column("messages", "secret_conv_id")
    op.drop_column("messages", "secret_duration")


def downgrade() -> None:
    op.add_column("conversations", sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("conversations", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("secret_conv_id", sa.UUID(), nullable=True))
    op.add_column("messages", sa.Column("secret_duration", sa.Integer(), nullable=True))
