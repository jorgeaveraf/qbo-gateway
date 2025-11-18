from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import CHAR, TypeDecorator


class GUID(TypeDecorator):
    """Platform-independent GUID type."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


class Base(DeclarativeBase):
    """Shared base class for ORM models."""

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        primary_key=True,
        default=uuid.uuid4,
    )


class ClientStatus(str):
    ACTIVE = "active"
    INACTIVE = "inactive"


class Clients(Base):
    __tablename__ = "clients"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            ClientStatus.ACTIVE,
            ClientStatus.INACTIVE,
            name="client_status_enum",
            native_enum=False,
        ),
        default=ClientStatus.ACTIVE,
        nullable=False,
    )
    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        "metadata",
        JSON(none_as_null=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    credentials: Mapped[list["ClientCredentials"]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
    )


class ClientCredentials(Base):
    __tablename__ = "client_credentials"
    __table_args__ = (
        UniqueConstraint("client_id", "environment", name="uq_client_environment"),
        UniqueConstraint("realm_id", "environment", name="uq_realm_environment"),
        Index("ix_client_credentials_client_id", "client_id"),
        Index("ix_client_credentials_realm_id", "realm_id"),
        Index(
            "ix_client_credentials_client_env_access_exp",
            "client_id",
            "environment",
            "access_expires_at",
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    realm_id: Mapped[str] = mapped_column(String(64), nullable=False)
    refresh_token_enc: Mapped[str] = mapped_column(String, nullable=False)
    access_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    access_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    refresh_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    environment: Mapped[str] = mapped_column(
        Enum("sandbox", "prod", name="environment_enum", native_enum=False),
        default="sandbox",
        nullable=False,
    )
    scopes: Mapped[Optional[list[str]]] = mapped_column(JSON(none_as_null=True))
    refresh_counter: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    client: Mapped["Clients"] = relationship(back_populates="credentials")


class IdempotencyKeys(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("key", name="uq_idempotency_key"),
        Index("ix_idempotency_keys_client_id", "client_id"),
    )

    client_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=True,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    response_body: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
