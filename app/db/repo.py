from __future__ import annotations

import uuid
from typing import Iterable, Optional

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClientCredentials, Clients, IdempotencyKeys
from app.schemas.client import ClientCreate, ClientUpdate, Environment


async def create_client(session: AsyncSession, payload: ClientCreate) -> Clients:
    client = Clients(
        name=payload.name,
        status=payload.status,
        metadata_json=payload.metadata,
    )
    session.add(client)
    await session.flush()
    await session.refresh(client)
    return client


async def list_clients(session: AsyncSession) -> Iterable[Clients]:
    result = await session.execute(
        select(Clients).order_by(Clients.created_at.desc())
    )
    return result.scalars().all()


async def get_client_by_id(session: AsyncSession, client_id: uuid.UUID) -> Clients:
    result = await session.execute(
        select(Clients).where(Clients.id == client_id)
    )
    client = result.scalar_one_or_none()
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found",
        )
    return client


async def update_client(
    session: AsyncSession,
    client: Clients,
    payload: ClientUpdate,
) -> Clients:
    if payload.name is not None:
        client.name = payload.name
    if payload.status is not None:
        client.status = payload.status
    if payload.metadata is not None:
        client.metadata_json = payload.metadata
    await session.flush()
    await session.refresh(client)
    return client


async def delete_client(session: AsyncSession, client: Clients) -> None:
    await session.delete(client)
    await session.flush()


async def get_credentials(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    environment: Optional[Environment] = None,
) -> Iterable[ClientCredentials]:
    stmt = select(ClientCredentials).where(ClientCredentials.client_id == client_id)
    if environment:
        stmt = stmt.where(ClientCredentials.environment == environment)
    stmt = stmt.order_by(ClientCredentials.created_at.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_credential_by_id(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    credential_id: uuid.UUID,
) -> ClientCredentials:
    result = await session.execute(
        select(ClientCredentials).where(
            ClientCredentials.client_id == client_id,
            ClientCredentials.id == credential_id,
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Credential not found",
        )
    return credential


async def get_credential_by_client_and_env(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    environment: Environment,
) -> ClientCredentials:
    result = await session.execute(
        select(ClientCredentials).where(
            ClientCredentials.client_id == client_id,
            ClientCredentials.environment == environment,
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Credential not found",
        )
    return credential


async def get_credential_optional(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    environment: Environment,
) -> Optional[ClientCredentials]:
    result = await session.execute(
        select(ClientCredentials).where(
            ClientCredentials.client_id == client_id,
            ClientCredentials.environment == environment,
        )
    )
    return result.scalar_one_or_none()


async def save_credential(session: AsyncSession, credential: ClientCredentials) -> ClientCredentials:
    session.add(credential)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Credential conflict",
        ) from exc
    await session.refresh(credential)
    return credential


async def delete_idempotency_records_for_client(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
) -> None:
    await session.execute(
        delete(IdempotencyKeys).where(IdempotencyKeys.client_id == client_id)
    )
    await session.flush()
