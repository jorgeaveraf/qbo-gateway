"""Allow nullable client_id for idempotency keys

Revision ID: 20241031_0002
Revises: 20241029_0001
Create Date: 2025-10-31 18:30:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20241031_0002"
down_revision = "20241029_0001"
branch_labels = None
depends_on = None


def _guid_type(bind) -> sa.types.TypeEngine:
    if bind.dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    guid = _guid_type(bind)
    op.alter_column(
        "idempotency_keys",
        "client_id",
        existing_type=guid,
        nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    guid = _guid_type(bind)
    null_client_ids = bind.execute(
        sa.text("SELECT COUNT(*) FROM idempotency_keys WHERE client_id IS NULL")
    ).scalar_one()
    if null_client_ids:
        raise RuntimeError(
            "Cannot downgrade while idempotency_keys.client_id contains NULL values."
        )
    op.alter_column(
        "idempotency_keys",
        "client_id",
        existing_type=guid,
        nullable=False,
    )
