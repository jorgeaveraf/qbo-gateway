from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, Field


ClientStatus = Literal["active", "inactive"]
Environment = Literal["sandbox", "prod"]


class ClientBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    status: ClientStatus = "active"
    metadata: Optional[dict[str, Any]] = Field(
        default=None,
        validation_alias=AliasChoices("metadata_json", "metadata"),
    )


class ClientCreate(ClientBase):
    pass


class ClientUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    status: Optional[ClientStatus] = None
    metadata: Optional[dict[str, Any]] = None


class ClientRead(ClientBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CredentialSummary(BaseModel):
    id: uuid.UUID
    realm_id: str
    environment: Environment
    access_expires_at: Optional[datetime]
    refresh_expires_at: Optional[datetime]
    scopes: list[str] | None = None
    refresh_counter: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ClientWithCredentials(ClientRead):
    credentials: list[CredentialSummary] = []


class CredentialRotateResponse(BaseModel):
    client_id: uuid.UUID
    credential_id: uuid.UUID
    refreshed: bool
    access_expires_at: datetime
    refresh_expires_at: datetime


class CredentialListResponse(BaseModel):
    client_id: uuid.UUID
    credentials: list[CredentialSummary]
