from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import logging as logging_utils
from app.core.config import Settings, get_settings
from app.core.security import decode_oauth_state, encode_oauth_state
from app.db import repo
from app.db.session import get_session
from app.schemas.client import ClientRead
from app.services.qbo_client import QuickBooksOAuthError, QuickBooksService
from app.utils.validators import parse_uuid, resolve_environment


router = APIRouter(prefix="/auth", tags=["auth"])
public_router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("app.api.auth")


@router.get("/connect", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
async def connect_oauth(
    client_id: str,
    env: str = Query(default="sandbox"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    client_uuid = parse_uuid(client_id, "client_id")
    environment = resolve_environment(env, settings.environment)
    logging_utils.set_request_context(client_id=str(client_uuid))

    client = await repo.get_client_by_id(session, client_uuid)
    if client.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client is inactive",
        )

    state_payload = {
        "client_id": str(client_uuid),
        "environment": environment,
        "nonce": str(uuid.uuid4()),
    }
    state = encode_oauth_state(settings.fernet_key, state_payload)

    qbo_service = QuickBooksService(settings)
    auth_url = qbo_service.build_authorization_url(state=state, environment=environment)
    logger.info(
        "oauth_connect_redirect",
        extra={"client_id": str(client_uuid), "environment": environment},
    )
    return RedirectResponse(auth_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@public_router.get("/callback")
async def oauth_callback(
    code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    realmId: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
    error_description: Optional[str] = Query(default=None),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
):
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OAuth error: {error_description or error}",
        )
    if not code or not state or not realmId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required OAuth parameters",
        )

    try:
        state_payload = decode_oauth_state(settings.fernet_key, state)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    client_uuid = parse_uuid(state_payload.get("client_id", ""), "client_id")
    environment = resolve_environment(state_payload.get("environment"), settings.environment)
    logging_utils.set_request_context(client_id=str(client_uuid))

    client = await repo.get_client_by_id(session, client_uuid)
    qbo_service = QuickBooksService(settings)

    try:
        token_bundle = await qbo_service.exchange_authorization_code(code=code, realm_id=realmId)
    except QuickBooksOAuthError as exc:
        logger.error(
            "oauth_exchange_failed",
            extra={
                "client_id": str(client_uuid),
                "environment": environment,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to exchange authorization code",
        ) from exc

    credential = await qbo_service.upsert_credentials(
        session,
        client=client,
        environment=environment,
        realm_id=realmId,
        bundle=token_bundle,
    )
    await session.commit()

    logger.info(
        "oauth_callback_completed",
        extra={
            "client_id": str(client_uuid),
            "realm_id": realmId,
            "environment": environment,
        },
    )

    response_payload = {
        "message": "OAuth flow completed",
        "client": ClientRead.model_validate(client).model_dump(),
        "credential_id": str(credential.id),
        "realm_id": realmId,
        "environment": environment,
        "access_expires_at": credential.access_expires_at,
        "refresh_expires_at": credential.refresh_expires_at,
        "scopes": credential.scopes,
    }
    return response_payload
