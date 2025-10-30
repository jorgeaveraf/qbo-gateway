from __future__ import annotations

from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import logging as logging_utils
from app.core.config import Settings, get_settings
from app.db import repo
from app.db.session import get_session
from app.schemas.qbo import QBOProxyResponse
from app.services.qbo_client import QuickBooksApiError, QuickBooksService
from app.utils.validators import parse_uuid, resolve_environment


router = APIRouter(prefix="/qbo", tags=["qbo"])
logger = logging.getLogger("app.api.qbo")


@router.get("/{client_id}/companyinfo", response_model=QBOProxyResponse)
async def get_company_info(
    client_id: str,
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    client_uuid = parse_uuid(client_id, "client_id")
    env = resolve_environment(environment, settings.environment)
    logging_utils.set_request_context(client_id=str(client_uuid))

    client = await repo.get_client_by_id(session, client_uuid)
    if client.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client is inactive",
        )

    credential = await repo.get_credential_by_client_and_env(
        session,
        client_id=client_uuid,
        environment=env,
    )
    logging_utils.set_request_context(client_id=str(client_uuid), realm_id=credential.realm_id)

    qbo_service = QuickBooksService(settings)
    try:
        payload, refreshed, latency_ms = await qbo_service.fetch_company_info(session, credential)
    except QuickBooksApiError as exc:
        logger.error(
            "qbo_proxy_error",
            extra={
                "client_id": str(client_uuid),
                "realm_id": credential.realm_id,
                "environment": env,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="QuickBooks API error",
        ) from exc

    await session.commit()

    logger.info(
        "qbo_companyinfo_success",
        extra={
            "client_id": str(client_uuid),
            "realm_id": credential.realm_id,
            "environment": env,
            "refreshed": refreshed,
            "latency_ms": round(latency_ms, 2),
        },
    )

    return QBOProxyResponse(
        client_id=str(client_uuid),
        realm_id=credential.realm_id,
        environment=env,
        fetched_at=datetime.now(timezone.utc),
        latency_ms=round(latency_ms, 2),
        data=payload,
        refreshed=refreshed,
    )
