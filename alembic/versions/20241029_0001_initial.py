"""Initial database schema

Revision ID: 20241029_0001
Revises: 
Create Date: 2025-10-29 18:05:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20241029_0001"
down_revision = None
branch_labels = None
depends_on = None


client_status_enum = sa.Enum(
    "active",
    "inactive",
    name="client_status_enum",
    native_enum=False,
)

environment_enum = sa.Enum(
    "sandbox",
    "prod",
    name="environment_enum",
    native_enum=False,
)


def _guid_type(bind) -> sa.types.TypeEngine:
    if bind.dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    guid = _guid_type(bind)

    client_status_enum.create(op.get_bind(), checkfirst=True)
    environment_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "clients",
        sa.Column("id", guid, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", client_status_enum, nullable=False, server_default="active"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "client_credentials",
        sa.Column("id", guid, nullable=False),
        sa.Column("client_id", guid, nullable=False),
        sa.Column("realm_id", sa.String(length=64), nullable=False),
        sa.Column("refresh_token_enc", sa.String(), nullable=False),
        sa.Column("access_token", sa.String(), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("environment", environment_enum, nullable=False, server_default="sandbox"),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("refresh_counter", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "environment", name="uq_client_environment"),
        sa.UniqueConstraint("realm_id", "environment", name="uq_realm_environment"),
    )
    op.create_index(
        "ix_client_credentials_client_id",
        "client_credentials",
        ["client_id"],
    )
    op.create_index(
        "ix_client_credentials_realm_id",
        "client_credentials",
        ["realm_id"],
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("id", guid, nullable=False),
        sa.Column("client_id", guid, nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_idempotency_key"),
    )
    op.create_index(
        "ix_idempotency_keys_client_id",
        "idempotency_keys",
        ["client_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_client_id", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_client_credentials_realm_id", table_name="client_credentials")
    op.drop_index("ix_client_credentials_client_id", table_name="client_credentials")
    op.drop_table("client_credentials")
    op.drop_table("clients")
    environment_enum.drop(op.get_bind(), checkfirst=True)
    client_status_enum.drop(op.get_bind(), checkfirst=True)
