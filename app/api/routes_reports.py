from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes_qbo import _get_client_context
from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.schemas.qbo import QBOProxyResponse
from app.services.qbo_client import QuickBooksApiError, QuickBooksService
from app.utils.validators import resolve_environment_optional


router = APIRouter(prefix="/qbo/{client_id}/reports", tags=["reports"])
logger = logging.getLogger("app.api.reports")


@dataclass
class ReportQueryParams:
    environment: str | None
    report_date: date | None
    date_macro: str | None
    aging_period: int | None
    num_periods: int | None


def get_report_query_params(
    environment: str | None = Query(default=None),
    report_date: date | None = Query(default=None),
    date_macro: str | None = Query(default=None),
    aging_period: int | None = Query(default=None, ge=1),
    num_periods: int | None = Query(default=None, ge=1),
) -> ReportQueryParams:
    return ReportQueryParams(
        environment=resolve_environment_optional(environment),
        report_date=report_date,
        date_macro=date_macro,
        aging_period=aging_period,
        num_periods=num_periods,
    )


@router.get(
    "/ar-aging-summary",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    summary="AR Aging Summary",
    description="Proxies the QuickBooks Aged Receivables report for the given client.",
)
async def get_ar_aging_summary(
    client_id: str,
    params: ReportQueryParams = Depends(get_report_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    return await _fetch_report(
        report_name="AgedReceivables",
        client_id=client_id,
        params=params,
        session=session,
        settings=settings,
    )


@router.get(
    "/ap-aging-summary",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    summary="AP Aging Summary",
    description="Proxies the QuickBooks Aged Payables report for the given client.",
)
async def get_ap_aging_summary(
    client_id: str,
    params: ReportQueryParams = Depends(get_report_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    return await _fetch_report(
        report_name="AgedPayables",
        client_id=client_id,
        params=params,
        session=session,
        settings=settings,
    )


async def _fetch_report(
    *,
    report_name: str,
    client_id: str,
    params: ReportQueryParams,
    session: AsyncSession,
    settings: Settings,
) -> QBOProxyResponse:
    report_params = _build_report_params(params)
    client_uuid, env, credential = await _get_client_context(client_id, params.environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)

    try:
        payload, refreshed, latency_ms = await qbo_service.fetch_report(
            session,
            credential,
            report_name=report_name,
            params=report_params,
        )
    except QuickBooksApiError as exc:
        logger.error(
            "qbo_report_error",
            extra={
                "client_id": client_id_str,
                "realm_id": credential.realm_id,
                "environment": env,
                "report": report_name,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="QuickBooks API error",
        ) from exc

    await session.commit()
    logger.info(
        "qbo_report_success",
        extra={
            "client_id": client_id_str,
            "realm_id": credential.realm_id,
            "environment": env,
            "report": report_name,
            "refreshed": refreshed,
            "latency_ms": round(latency_ms, 2),
        },
    )

    return QBOProxyResponse(
        client_id=client_id_str,
        realm_id=credential.realm_id,
        environment=env,
        fetched_at=datetime.now(timezone.utc),
        latency_ms=round(latency_ms, 2),
        data=payload,
        refreshed=refreshed,
    )


def _build_report_params(params: ReportQueryParams) -> dict[str, Any]:
    if params.report_date and params.date_macro:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either report_date or date_macro, not both",
        )

    report_params: dict[str, Any] = {}
    if params.report_date:
        report_params["report_date"] = params.report_date.isoformat()
    if params.date_macro:
        report_params["date_macro"] = params.date_macro
    if params.aging_period is not None:
        report_params["aging_period"] = params.aging_period
    if params.num_periods is not None:
        report_params["num_periods"] = params.num_periods
    return report_params
