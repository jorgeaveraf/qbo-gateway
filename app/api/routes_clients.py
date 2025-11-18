from __future__ import annotations

import logging
import uuid

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core import logging as logging_utils
from app.db import repo
from app.db.session import get_session
from app.schemas.client import (
    ClientCreate,
    ClientListItemSummary,
    ClientRead,
    ClientUpdate,
    ClientWithCredentials,
    CredentialListResponse,
    CredentialRotateResponse,
    CredentialSummary,
)
from app.services.qbo_client import QuickBooksApiError, QuickBooksOAuthError, QuickBooksService
from app.utils.idempotency import register_idempotency_key, store_idempotent_response
from app.utils.validators import parse_uuid, resolve_environment, resolve_environment_optional


router = APIRouter(prefix="/clients", tags=["clients"])

logger = logging.getLogger("app.api.clients")
CLIENT_LIST_RESPONSE_EXAMPLES = {
    "default": {
        "summary": "Default listing",
        "value": [
            {
                "id": "477751f2-1142-4283-a3c0-387f8aa45fbd",
                "name": "ACME Sandbox",
                "status": "active",
                "metadata": {"tier": "sandbox"},
                "created_at": "2025-11-06T03:49:05.703363Z",
                "updated_at": "2025-11-06T03:49:05.703363Z",
            }
        ],
    },
    "summary": {
        "summary": "Summary mode",
        "value": [
            {
                "id": "477751f2-1142-4283-a3c0-387f8aa45fbd",
                "name": "ACME Sandbox",
                "status": "active",
                "metadata": {"tier": "sandbox"},
                "created_at": "2025-11-06T03:49:05.703363Z",
                "updated_at": "2025-11-06T03:49:05.703363Z",
                "has_credentials": True,
                "environments": ["sandbox"],
                "access_status": "valid",
                "access_expires_at": "2025-11-09T03:09:20.002614Z",
            }
        ],
    },
    "summary_env": {
        "summary": "Summary filtered by env",
        "value": [
            {
                "id": "eb8f107d-4b23-45aa-b9b5-7ce8311ec98b",
                "name": "Test Client",
                "status": "active",
                "metadata": {"notes": "Cliente demo"},
                "created_at": "2025-11-06T03:41:37.841377Z",
                "updated_at": "2025-11-06T03:41:37.841377Z",
                "has_credentials": False,
                "environments": [],
                "access_status": "none",
                "access_expires_at": None,
            }
        ],
    },
}


@router.post(
    "",
    response_model=ClientRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_client(
    payload: ClientCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
) -> ClientRead:
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    placeholder_client_id = uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)

    record, reused = await register_idempotency_key(
        session,
        client_id=placeholder_client_id,
        key=idempotency_key,
        request_payload=payload.model_dump(),
        resource_type="client:create",
    )

    if reused and record.response_body:
        logging_utils.set_request_context(client_id=str(record.client_id))
        return ClientRead.model_validate(record.response_body)

    client = await repo.create_client(session, payload)
    logging_utils.set_request_context(client_id=str(client.id))
    record.client_id = client.id
    response_model = ClientRead.model_validate(client)
    response_body = jsonable_encoder(response_model)
    await store_idempotent_response(session, record, response_body)
    await session.commit()

    logger.info(
        "client_created",
        extra={"client_id": str(client.id), "status": client.status},
    )
    return response_model


@router.get(
    "",
    response_model=list[ClientRead | ClientListItemSummary],
    description=(
        "Lists clients. Set `summary=true` to enrich each record with credential status "
        "metadata and optionally limit the aggregation to a specific environment via `env`."
    ),
    responses={
        200: {
            "content": {
                "application/json": {
                    "examples": CLIENT_LIST_RESPONSE_EXAMPLES,
                }
            }
        }
    },
)
async def list_clients(
    summary: bool = Query(
        default=False,
        description="Include credential summary metadata for each client.",
    ),
    env: str | None = Query(
        default=None,
        description="Environment filter (sandbox or production) applied when summary=true.",
    ),
    session: AsyncSession = Depends(get_session),
) -> list[ClientRead | ClientListItemSummary]:
    if not summary:
        clients = await repo.list_clients(session)
        return [ClientRead.model_validate(client) for client in clients]

    resolved_env = resolve_environment_optional(env)
    aggregates = await repo.list_clients_with_summary(
        session,
        environment=resolved_env,
    )
    now = datetime.now(timezone.utc)
    summarized: list[ClientListItemSummary] = []
    for aggregate in aggregates:
        client_data = ClientRead.model_validate(aggregate.client)
        has_credentials = aggregate.credentials_count > 0
        expires_at = aggregate.access_expires_at
        if not has_credentials:
            access_status = "none"
        elif expires_at and expires_at > now:
            access_status = "valid"
        else:
            access_status = "expired"
        summary_model = ClientListItemSummary(
            **client_data.model_dump(),
            has_credentials=has_credentials,
            environments=aggregate.environments,
            access_status=access_status,
            access_expires_at=expires_at,
        )
        summarized.append(summary_model)
    return summarized


@router.get("/{client_id}", response_model=ClientWithCredentials)
async def get_client(
    client_id: str,
    session: AsyncSession = Depends(get_session),
) -> ClientWithCredentials:
    client_uuid = parse_uuid(client_id, "client_id")
    logging_utils.set_request_context(client_id=str(client_uuid))
    client = await repo.get_client_by_id(session, client_uuid)
    credentials = await repo.get_credentials(session, client_id=client_uuid, environment=None)
    payload = ClientWithCredentials(
        **ClientRead.model_validate(client).model_dump(),
        credentials=[CredentialSummary.model_validate(item) for item in credentials],
    )
    return payload


@router.patch("/{client_id}", response_model=ClientRead)
async def update_client(
    client_id: str,
    payload: ClientUpdate,
    session: AsyncSession = Depends(get_session),
) -> ClientRead:
    client_uuid = parse_uuid(client_id, "client_id")
    logging_utils.set_request_context(client_id=str(client_uuid))
    client = await repo.get_client_by_id(session, client_uuid)
    updated = await repo.update_client(session, client, payload)
    await session.commit()
    logger.info(
        "client_updated",
        extra={"client_id": str(client_uuid)},
    )
    return ClientRead.model_validate(updated)


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    client_uuid = parse_uuid(client_id, "client_id")
    logging_utils.set_request_context(client_id=str(client_uuid))
    client = await repo.get_client_by_id(session, client_uuid)
    await repo.delete_client(session, client)
    await session.commit()
    logger.info("client_deleted", extra={"client_id": str(client_uuid)})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{client_id}/credentials", response_model=CredentialListResponse)
async def get_client_credentials(
    client_id: str,
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> CredentialListResponse:
    client_uuid = parse_uuid(client_id, "client_id")
    env = resolve_environment(environment, settings.environment)
    logging_utils.set_request_context(client_id=str(client_uuid))
    await repo.get_client_by_id(session, client_uuid)
    credentials = await repo.get_credentials(session, client_id=client_uuid, environment=env)
    return CredentialListResponse(
        client_id=client_uuid,
        credentials=[CredentialSummary.model_validate(item) for item in credentials],
    )


@router.post(
    "/{client_id}/credentials/rotate",
    response_model=CredentialRotateResponse,
)
async def rotate_client_credentials(
    client_id: str,
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> CredentialRotateResponse:
    client_uuid = parse_uuid(client_id, "client_id")
    env = resolve_environment(environment, settings.environment)
    logging_utils.set_request_context(client_id=str(client_uuid))
    await repo.get_client_by_id(session, client_uuid)
    credential = await repo.get_credential_by_client_and_env(
        session,
        client_id=client_uuid,
        environment=env,
    )

    qbo_service = QuickBooksService(settings)
    try:
        await qbo_service.rotate_credential(session, credential)
    except QuickBooksOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except QuickBooksApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"QuickBooks API error: {exc}",
        ) from exc
    await session.commit()

    logger.info(
        "credential_rotated",
        extra={
            "client_id": str(client_uuid),
            "credential_id": str(credential.id),
            "environment": env,
        },
    )

    return CredentialRotateResponse(
        client_id=client_uuid,
        credential_id=credential.id,
        refreshed=True,
        access_expires_at=credential.access_expires_at,
        refresh_expires_at=credential.refresh_expires_at,
    )
