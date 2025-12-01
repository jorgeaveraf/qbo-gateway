from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Any, Iterable, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import logging as logging_utils
from app.core.config import Settings, get_settings
from app.db import repo
from app.db.models import ClientCredentials
from app.db.session import get_session
from app.schemas.qbo import (
    AccountUpdate,
    BillCreate,
    BillLine,
    BillPaymentCreate,
    BillPaymentLine,
    QBOAccountDetailResponse,
    QBOAccountListResponse,
    CustomerCreate,
    DepositCreate,
    DepositLine,
    ExpenseCreate,
    ExpenseLine,
    InvoiceCreate,
    InvoiceLine,
    ItemCreate,
    PaymentCreate,
    PaymentLine,
    QBODocumentLine,
    QBOListResponse,
    QBOProxyResponse,
    SalesReceiptCreate,
    SalesReceiptLine,
    VendorCreate,
)
from app.services.qbo_client import QuickBooksApiError, QuickBooksOAuthError, QuickBooksService
from app.services.qbo_refs import QBOReferenceResolver
from app.utils.idempotency import build_fingerprint, register_idempotency_key, store_idempotent_response
from app.utils.validators import (
    normalize_max_results,
    normalize_start_position,
    parse_uuid,
    resolve_environment,
    resolve_environment_optional,
)


router = APIRouter(prefix="/qbo", tags=["qbo"])
logger = logging.getLogger("app.api.qbo")


@dataclass(frozen=True)
class QueryEntityConfig:
    table: str
    result_key: str
    order_by: Optional[str]
    date_field: Optional[str]
    updated_field: Optional[str]
    customer_field: Optional[str]
    vendor_field: Optional[str]
    doc_field: Optional[str]
    status_field: Optional[str]
    extra_filters: tuple[str, ...] = ()


QUERY_ENTITIES: dict[str, QueryEntityConfig] = {
    "customers": QueryEntityConfig(
        table="Customer",
        result_key="Customer",
        order_by="MetaData.LastUpdatedTime DESC",
        date_field="MetaData.CreateTime",
        updated_field="MetaData.LastUpdatedTime",
        customer_field=None,
        vendor_field=None,
        doc_field=None,
        status_field="Active",
    ),
    "vendors": QueryEntityConfig(
        table="Vendor",
        result_key="Vendor",
        order_by="MetaData.LastUpdatedTime DESC",
        date_field="MetaData.CreateTime",
        updated_field="MetaData.LastUpdatedTime",
        customer_field=None,
        vendor_field=None,
        doc_field=None,
        status_field="Active",
    ),
    "items": QueryEntityConfig(
        table="Item",
        result_key="Item",
        order_by="MetaData.LastUpdatedTime DESC",
        date_field="MetaData.CreateTime",
        updated_field="MetaData.LastUpdatedTime",
        customer_field=None,
        vendor_field=None,
        doc_field=None,
        status_field="Active",
    ),
    "accounts": QueryEntityConfig(
        table="Account",
        result_key="Account",
        order_by="FullyQualifiedName ASC",
        date_field=None,
        updated_field="MetaData.LastUpdatedTime",
        customer_field=None,
        vendor_field=None,
        doc_field=None,
        status_field=None,
    ),
    "invoices": QueryEntityConfig(
        table="Invoice",
        result_key="Invoice",
        order_by="TxnDate DESC",
        date_field="TxnDate",
        updated_field="MetaData.LastUpdatedTime",
        customer_field="CustomerRef",
        vendor_field=None,
        doc_field="DocNumber",
        status_field="TxnStatus",
    ),
    "payments": QueryEntityConfig(
        table="Payment",
        result_key="Payment",
        order_by="TxnDate DESC",
        date_field="TxnDate",
        updated_field="MetaData.LastUpdatedTime",
        customer_field="CustomerRef",
        vendor_field=None,
        doc_field="PaymentRefNum",
        status_field="TxnStatus",
    ),
    "salesreceipts": QueryEntityConfig(
        table="SalesReceipt",
        result_key="SalesReceipt",
        order_by="TxnDate DESC",
        date_field="TxnDate",
        updated_field="MetaData.LastUpdatedTime",
        customer_field="CustomerRef",
        vendor_field=None,
        doc_field="DocNumber",
        status_field="TxnStatus",
    ),
    "expenses": QueryEntityConfig(
        table="Purchase",
        result_key="Purchase",
        order_by="TxnDate DESC",
        date_field="TxnDate",
        updated_field="MetaData.LastUpdatedTime",
        customer_field=None,
        vendor_field="EntityRef",
        doc_field="DocNumber",
        status_field="TxnStatus",
        extra_filters=("PaymentType = 'Cash'",),
    ),
    "bills": QueryEntityConfig(
        table="Bill",
        result_key="Bill",
        order_by="TxnDate DESC",
        date_field="TxnDate",
        updated_field="MetaData.LastUpdatedTime",
        customer_field=None,
        vendor_field="VendorRef",
        doc_field="DocNumber",
        status_field="TxnStatus",
    ),
    "billpayments": QueryEntityConfig(
        table="BillPayment",
        result_key="BillPayment",
        order_by="TxnDate DESC",
        date_field="TxnDate",
        updated_field="MetaData.LastUpdatedTime",
        customer_field=None,
        vendor_field="VendorRef",
        doc_field="DocNumber",
        status_field="TxnStatus",
    ),
    "deposits": QueryEntityConfig(
        table="Deposit",
        result_key="Deposit",
        order_by="TxnDate DESC",
        date_field="TxnDate",
        updated_field="MetaData.LastUpdatedTime",
        customer_field=None,
        vendor_field=None,
        doc_field="DocNumber",
        status_field="TxnStatus",
    ),
}


@dataclass
class CollectionQueryParams:
    environment: str | None
    updated_since: datetime | None
    date_from: date | None
    date_to: date | None
    startposition: int | None
    maxresults: int | None
    customer_ref: str | None
    vendor_ref: str | None
    doc_number: str | None
    status: str | None


def get_collection_query_params(
    environment: str | None = Query(default=None),
    updated_since: datetime | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    startposition: int | None = Query(default=1, ge=1),
    maxresults: int | None = Query(default=100, ge=1, le=1000),
    customer_ref: str | None = Query(default=None),
    vendor_ref: str | None = Query(default=None),
    doc_number: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> CollectionQueryParams:
    return CollectionQueryParams(
        environment=environment,
        updated_since=updated_since,
        date_from=date_from,
        date_to=date_to,
        startposition=startposition,
        maxresults=maxresults,
        customer_ref=customer_ref,
        vendor_ref=vendor_ref,
        doc_number=doc_number,
        status=status,
    )


@dataclass
class AccountsQueryParams:
    environment: str | None
    updated_since: datetime | None
    account_type: str | None
    classification: str | None
    active: bool | None
    startposition: int | None
    maxresults: int | None


def get_accounts_query_params(
    environment: str | None = Query(default=None),
    updated_since: datetime | None = Query(default=None),
    account_type: str | None = Query(default=None),
    classification: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    startposition: int | None = Query(default=1, ge=1),
    maxresults: int | None = Query(default=100, ge=1, le=1000),
) -> AccountsQueryParams:
    return AccountsQueryParams(
        environment=resolve_environment_optional(environment),
        updated_since=updated_since,
        account_type=account_type,
        classification=classification,
        active=active,
        startposition=startposition,
        maxresults=maxresults,
    )


@router.post(
    "/{client_id}/deposits",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create Deposit",
    description=(
        "Creates a bank Deposit consolidating multiple income lines; each line may include an optional "
        "Received From entity (customer/vendor/employee/other)."
    ),
)
async def create_deposit(
    client_id: str,
    payload: DepositCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auto_create: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    allow_auto_create = auto_create or env == "sandbox"
    deposit_account_ref = await resolver.ensure_account(
        payload.deposit_to_account,
        account_type="Bank",
        account_sub_type="Checking",
        auto_create=allow_auto_create,
    )
    line_payloads, total_amount, line_descriptor = await _build_deposit_lines(
        payload.lines,
        resolver,
        auto_create_accounts=allow_auto_create,
        auto_create_entities=allow_auto_create,
    )

    deposit_payload: dict[str, Any] = {
        "TxnDate": payload.date.isoformat(),
        "DepositToAccountRef": deposit_account_ref,
        "Line": line_payloads,
    }
    if payload.private_note:
        deposit_payload["PrivateNote"] = payload.private_note
    if payload.doc_number:
        deposit_payload["DocNumber"] = payload.doc_number
    if payload.class_name:
        class_ref = await resolver.resolve_class(payload.class_name)
        deposit_payload["ClassRef"] = class_ref

    fingerprint = build_fingerprint(
        credential.realm_id,
        "Deposit",
        payload.date.isoformat(),
        total_amount,
        deposit_account_ref["value"],
        payload.txn_id or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="deposit:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="Deposit",
        resource="deposit",
        payload=deposit_payload,
    )


@router.post(
    "/{client_id}/payments",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create Payment",
    description="Applies customer payments to open invoices and enforces idempotency based on doc references.",
)
async def create_payment(
    client_id: str,
    payload: PaymentCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auto_create: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    customer_ref = await resolver.resolve_customer(payload.customer, auto_create=auto_create)
    line_payloads, total_amount, ref_docs = await _build_payment_lines(payload.lines, resolver)

    payment_payload: dict[str, Any] = {
        "TxnDate": payload.date.isoformat(),
        "CustomerRef": customer_ref,
        "Line": line_payloads,
        "TotalAmt": float(total_amount),
    }
    if payload.doc_number:
        payment_payload["PaymentRefNum"] = payload.doc_number
    if payload.private_note:
        payment_payload["PrivateNote"] = payload.private_note
    if payload.deposit_to_account:
        payment_payload["DepositToAccountRef"] = await resolver.resolve_account(payload.deposit_to_account)
    if payload.ar_account:
        payment_payload["ARAccountRef"] = await resolver.resolve_account(payload.ar_account)

    fingerprint = build_fingerprint(
        credential.realm_id,
        "Payment",
        payload.date.isoformat(),
        total_amount,
        customer_ref["value"],
        ref_docs,
        payload.txn_id or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="payment:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="Payment",
        resource="payment",
        payload=payment_payload,
    )


@router.post(
    "/{client_id}/billpayments",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create BillPayment",
    description="Pays one or more Bills using a bank or credit card account.",
)
async def create_billpayment(
    client_id: str,
    payload: BillPaymentCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auto_create: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    vendor_ref = await resolver.resolve_vendor(payload.vendor, auto_create=auto_create)
    line_payloads, total_amount, ref_docs = await _build_billpayment_lines(payload.lines, resolver)

    account_type = "Credit Card" if payload.payment_type == "CreditCard" else "Bank"
    payment_account_ref = await resolver.resolve_account(payload.bank_account, account_type=account_type)

    billpayment_payload: dict[str, Any] = {
        "TxnDate": payload.date.isoformat(),
        "VendorRef": vendor_ref,
        "PayType": payload.payment_type,
        "Line": line_payloads,
        "TotalAmt": float(total_amount),
    }
    if payload.doc_number:
        billpayment_payload["DocNumber"] = payload.doc_number
    if payload.private_note:
        billpayment_payload["PrivateNote"] = payload.private_note
    if payload.ap_account:
        billpayment_payload["APAccountRef"] = await resolver.resolve_account(payload.ap_account)
    if payload.payment_type == "CreditCard":
        billpayment_payload["CreditCardPayment"] = {"CCAccountRef": payment_account_ref}
    else:
        billpayment_payload["CheckPayment"] = {"BankAccountRef": payment_account_ref}

    fingerprint = build_fingerprint(
        credential.realm_id,
        "BillPayment",
        payload.date.isoformat(),
        total_amount,
        vendor_ref["value"],
        ref_docs,
        payload.txn_id or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="billpayment:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="BillPayment",
        resource="billpayment",
        payload=billpayment_payload,
    )


@router.post(
    "/{client_id}/customers",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create Customer",
    description="Creates or upserts a Customer master record with normalized contact info.",
)
async def create_customer(
    client_id: str,
    payload: CustomerCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header is required")
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    contact_payload = _build_contact_payload(payload)
    fingerprint = build_fingerprint(
        credential.realm_id,
        "Customer",
        payload.display_name,
        payload.email or payload.phone or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="customer:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="Customer",
        resource="customer",
        payload=contact_payload,
    )


@router.post(
    "/{client_id}/vendors",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create Vendor",
    description="Registers a Vendor (Accounts Payable contact) with the provided address, email and phone.",
)
async def create_vendor(
    client_id: str,
    payload: VendorCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header is required")
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    contact_payload = _build_contact_payload(payload)
    fingerprint = build_fingerprint(
        credential.realm_id,
        "Vendor",
        payload.display_name,
        payload.email or payload.phone or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="vendor:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="Vendor",
        resource="vendor",
        payload=contact_payload,
    )


@router.post(
    "/{client_id}/items",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create Item",
    description="Creates a product or service Item referencing the appropriate income/expense accounts.",
)
async def create_item(
    client_id: str,
    payload: ItemCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key header is required")
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    income_account = await resolver.resolve_account(payload.income_account)
    item_payload: dict[str, Any] = {
        "Name": payload.name,
        "Type": payload.type,
        "IncomeAccountRef": income_account,
        "Active": True if payload.active is None else payload.active,
    }
    if payload.description:
        item_payload["Description"] = payload.description
    if payload.sku:
        item_payload["Sku"] = payload.sku
    if payload.expense_account:
        item_payload["ExpenseAccountRef"] = await resolver.resolve_account(payload.expense_account)
    if payload.type == "Inventory":
        if payload.asset_account is None or payload.quantity_on_hand is None or payload.inventory_start_date is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="asset_account, quantity_on_hand, and inventory_start_date are required for inventory items",
            )
        asset_account = await resolver.resolve_account(payload.asset_account)
        item_payload["AssetAccountRef"] = asset_account
        item_payload["TrackQtyOnHand"] = True
        item_payload["QtyOnHand"] = float(payload.quantity_on_hand)
        item_payload["InvStartDate"] = payload.inventory_start_date.isoformat()
    else:
        item_payload["TrackQtyOnHand"] = False

    fingerprint = build_fingerprint(
        credential.realm_id,
        "Item",
        payload.name,
        payload.type,
        payload.sku or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="item:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="Item",
        resource="item",
        payload=item_payload,
    )


@router.get(
    "/{client_id}/accounts/{account_id}",
    response_model=QBOAccountDetailResponse,
    response_model_exclude_none=True,
    summary="Account Detail",
    description="Fetch a specific chart of accounts entry by Id, Name, or FullyQualifiedName.",
)
async def get_account_detail(
    client_id: str,
    account_id: str,
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOAccountDetailResponse:
    env_param = resolve_environment_optional(environment)
    client_uuid, env, credential = await _get_client_context(client_id, env_param, session, settings)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    try:
        account_payload, refreshed, latency_ms = await resolver.get_account(account_id)
    except QuickBooksApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="QuickBooks API error",
        ) from exc

    await session.commit()
    return QBOAccountDetailResponse(
        account=account_payload,
        latency_ms=round(latency_ms, 2),
        refreshed=refreshed,
    )


@router.patch(
    "/{client_id}/accounts/{account_id}",
    response_model=QBOAccountDetailResponse,
    response_model_exclude_none=True,
    summary="Update Account",
    description="Updates basic chart of accounts attributes such as name, account number, description, active flag, or parent.",
)
async def update_account(
    client_id: str,
    account_id: str,
    payload: AccountUpdate,
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOAccountDetailResponse:
    env_param = resolve_environment_optional(environment)
    client_uuid, env, credential = await _get_client_context(client_id, env_param, session, settings)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    account_record, refreshed_lookup, _ = await resolver.get_account(account_id)
    sync_token = account_record.get("SyncToken")
    if sync_token is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="QuickBooks account payload is missing SyncToken",
        )

    update_payload: dict[str, Any] = {
        "Id": account_record.get("Id"),
        "SyncToken": sync_token,
        "sparse": True,
        "Name": account_record.get("Name"),
        "AcctNum": account_record.get("AcctNum") or account_record.get("AccountNumber"),
        "Description": account_record.get("Description"),
        "Active": account_record.get("Active"),
        "AccountType": account_record.get("AccountType"),
        "AccountSubType": account_record.get("AccountSubType"),
        "Classification": account_record.get("Classification"),
    }
    if account_record.get("ParentRef"):
        update_payload["ParentRef"] = account_record["ParentRef"]
        update_payload["SubAccount"] = True

    if payload.name:
        update_payload["Name"] = payload.name
    if payload.account_number:
        update_payload["AcctNum"] = payload.account_number
    if payload.description:
        update_payload["Description"] = payload.description
    if payload.active is not None:
        update_payload["Active"] = payload.active
    if payload.parent_account:
        parent_record, _, _ = await resolver.get_account(payload.parent_account)
        parent_id = parent_record.get("Id")
        if parent_id == account_record.get("Id"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An account cannot be its own parent",
            )
        parent_ref = {"value": str(parent_id)}
        parent_name = parent_record.get("Name")
        if parent_name:
            parent_ref["name"] = str(parent_name)
        update_payload["ParentRef"] = parent_ref
        update_payload["SubAccount"] = True

    update_payload = {key: value for key, value in update_payload.items() if value is not None}

    try:
        data, refreshed_update, latency_ms, _ = await qbo_service.post(
            session,
            credential,
            entity="Account",
            resource="account?operation=update",
            payload=update_payload,
        )
    except QuickBooksApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="QuickBooks API error",
        ) from exc

    await session.commit()
    account_payload = data.get("Account") or data
    return QBOAccountDetailResponse(
        account=account_payload,
        latency_ms=round(latency_ms, 2),
        refreshed=refreshed_lookup or refreshed_update,
    )


@router.get("/{client_id}/companyinfo", response_model=QBOProxyResponse)
async def get_company_info(
    client_id: str,
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    try:
        payload, refreshed, latency_ms = await qbo_service.fetch_company_info(session, credential)
    except QuickBooksApiError as exc:
        logger.error(
            "qbo_proxy_error",
            extra={
                "client_id": client_id_str,
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
            "client_id": client_id_str,
            "realm_id": credential.realm_id,
            "environment": env,
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


async def _list_entity(
    entity_key: str,
    *,
    client_id: str,
    session: AsyncSession,
    environment: str | None,
    updated_since: datetime | None,
    date_from: date | None,
    date_to: date | None,
    startposition: int | None,
    maxresults: int | None,
    customer_ref: str | None,
    vendor_ref: str | None,
    doc_number: str | None,
    status_filter: str | None,
    active_filter: bool | None = None,
    account_type: str | None = None,
    classification: str | None = None,
    response_cls: type[QBOListResponse] = QBOListResponse,
    settings: Settings,
) -> QBOListResponse:
    config = QUERY_ENTITIES[entity_key]
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver: QBOReferenceResolver | None = None

    async def ensure_resolver() -> QBOReferenceResolver:
        nonlocal resolver
        if resolver is None:
            resolver = QBOReferenceResolver(qbo_service, session, credential)
        return resolver

    start = normalize_start_position(startposition)
    limit = normalize_max_results(maxresults)

    clauses: list[str] = list(config.extra_filters)
    if updated_since:
        if not config.updated_field:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="updated_since is not supported for this resource",
            )
        clauses.append(f"{config.updated_field} >= '{_format_datetime(updated_since)}'")
    if date_from:
        if not config.date_field:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="date_from is not supported for this resource",
            )
        clauses.append(f"{config.date_field} >= '{_format_date(date_from)}'")
    if date_to:
        if not config.date_field:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="date_to is not supported for this resource",
            )
        clauses.append(f"{config.date_field} <= '{_format_date(date_to)}'")
    if customer_ref:
        if not config.customer_field:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_ref filter is not supported for this resource",
            )
        resolved = await (await ensure_resolver()).resolve_customer(customer_ref)
        clauses.append(f"{config.customer_field} = '{_escape(resolved['value'])}'")
    if vendor_ref:
        if not config.vendor_field:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vendor_ref filter is not supported for this resource",
            )
        resolved = await (await ensure_resolver()).resolve_vendor(vendor_ref)
        clauses.append(f"{config.vendor_field} = '{_escape(resolved['value'])}'")
    if doc_number:
        if not config.doc_field:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="doc_number filter is not supported for this resource",
            )
        clauses.append(f"{config.doc_field} = '{_escape(doc_number)}'")
    if status_filter:
        if not config.status_field:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="status filter is not supported for this resource",
            )
        if config.status_field == "Active":
            normalized_status = status_filter.lower()
            if normalized_status not in {"active", "inactive"}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="status must be 'active' or 'inactive'",
                )
            clauses.append(f"{config.status_field} = {'true' if normalized_status == 'active' else 'false'}")
        else:
            clauses.append(f"{config.status_field} = '{_escape(status_filter)}'")
    if entity_key == "accounts":
        if account_type:
            clauses.append(f"AccountType = '{_escape(account_type)}'")
        if classification:
            clauses.append(f"Classification = '{_escape(classification)}'")
        if active_filter is not None:
            clauses.append(f"Active = {'true' if active_filter else 'false'}")

    sql = f"select * from {config.table}"
    if clauses:
        sql = f"{sql} where {' AND '.join(clauses)}"
    if config.order_by:
        sql = f"{sql} order by {config.order_by}"

    try:
        payload, refreshed, latency_ms = await qbo_service.query(
            session,
            credential,
            entity=config.table,
            select_sql=sql,
            startposition=start,
            maxresults=limit,
        )
    except QuickBooksApiError as exc:
        logger.error(
            "qbo_query_error",
            extra={
                "client_id": client_id_str,
                "realm_id": credential.realm_id,
                "entity": entity_key,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="QuickBooks API error",
        ) from exc

    await session.commit()

    query_response = payload.get("QueryResponse") or {}
    items = query_response.get(config.result_key) or []
    if isinstance(items, dict):
        items = [items]

    next_start = _compute_next_startposition(
        query_response,
        start_position=query_response.get("startPosition", start),
        max_results=limit,
        item_count=len(items),
    )

    logger.info(
        "qbo_query_success",
        extra={
            "client_id": client_id_str,
            "realm_id": credential.realm_id,
            "entity": entity_key,
            "items": len(items),
            "refreshed": refreshed,
            "next_start": next_start,
        },
    )

    return response_cls(
        items=items,
        next_startposition=next_start,
        latency_ms=round(latency_ms, 2),
        refreshed=refreshed,
    )


@router.get("/{client_id}/accounts", response_model=QBOAccountListResponse)
async def list_accounts(
    client_id: str,
    params: AccountsQueryParams = Depends(get_accounts_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOAccountListResponse:
    return await _list_entity(
        "accounts",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=None,
        date_to=None,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=None,
        vendor_ref=None,
        doc_number=None,
        status_filter=None,
        active_filter=params.active,
        account_type=params.account_type,
        classification=params.classification,
        response_cls=QBOAccountListResponse,
        settings=settings,
    )


@router.get("/{client_id}/customers", response_model=QBOListResponse)
async def list_customers(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "customers",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/vendors", response_model=QBOListResponse)
async def list_vendors(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "vendors",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/items", response_model=QBOListResponse)
async def list_items(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "items",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/invoices", response_model=QBOListResponse)
async def list_invoices(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "invoices",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/payments", response_model=QBOListResponse)
async def list_payments(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "payments",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/salesreceipts", response_model=QBOListResponse)
async def list_salesreceipts(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "salesreceipts",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/expenses", response_model=QBOListResponse)
async def list_expenses(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "expenses",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/bills", response_model=QBOListResponse)
async def list_bills(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "bills",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/billpayments", response_model=QBOListResponse)
async def list_billpayments(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "billpayments",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


@router.get("/{client_id}/deposits", response_model=QBOListResponse)
async def list_deposits(
    client_id: str,
    params: CollectionQueryParams = Depends(get_collection_query_params),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QBOListResponse:
    return await _list_entity(
        "deposits",
        client_id=client_id,
        session=session,
        environment=params.environment,
        updated_since=params.updated_since,
        date_from=params.date_from,
        date_to=params.date_to,
        startposition=params.startposition,
        maxresults=params.maxresults,
        customer_ref=params.customer_ref,
        vendor_ref=params.vendor_ref,
        doc_number=params.doc_number,
        status_filter=params.status,
        settings=settings,
    )


async def _execute_qbo_post(
    *,
    session: AsyncSession,
    credential: ClientCredentials,
    qbo_service: QuickBooksService,
    client_uuid: UUID,
    client_id_str: str,
    env: str,
    resource_type: str,
    idempotency_key: str,
    request_payload: dict[str, Any],
    fingerprint: str,
    entity: str,
    resource: str,
    payload: dict[str, Any],
    success_status_code: int = status.HTTP_201_CREATED,
) -> QBOProxyResponse:
    def _extract_txn_identifiers(payload_obj: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        txn_id = None
        doc_number = None
        for key in ("txn_id", "TxnId", "txnId"):
            if key in payload_obj:
                txn_id = payload_obj.get(key)
                break
        for key in ("doc_number", "DocNumber", "docNumber"):
            if key in payload_obj:
                doc_number = payload_obj.get(key)
                break
        return txn_id, doc_number

    def _classify_error(status_code: Optional[int], exc: Exception) -> str:
        if isinstance(exc, QuickBooksOAuthError):
            return "qbo_oauth"
        if status_code is None:
            return "unknown"
        if status_code >= 500:
            return "qbo_5xx"
        if status_code >= 400:
            return "qbo_4xx"
        return "unknown"

    def _truncate(body: Optional[str], limit: int = 400) -> Optional[str]:
        if body is None:
            return None
        if len(body) <= limit:
            return body
        return body[:limit] + "..."

    txn_id, doc_number = _extract_txn_identifiers(request_payload)
    txn_type = resource or entity.lower()
    logging_utils.log_qbo_txn_started(
        client_id=client_id_str,
        realm_id=credential.realm_id,
        environment=env,
        txn_type=txn_type,
        txn_id=txn_id,
        doc_number=doc_number,
        idempotency_key=idempotency_key,
        payload=request_payload,
    )

    record, reused = await register_idempotency_key(
        session,
        client_id=client_uuid,
        key=idempotency_key,
        request_payload=request_payload,
        resource_type=resource_type,
        fingerprint=fingerprint,
    )
    if reused and record.response_body:
        cached_body = record.response_body
        if not isinstance(cached_body, dict):
            cached_body = {}
        logging_utils.log_qbo_txn_finished(
            client_id=client_id_str,
            realm_id=credential.realm_id,
            environment=env,
            txn_type=txn_type,
            txn_id=txn_id,
            doc_number=doc_number,
            idempotency_key=idempotency_key,
            gateway_status_code=success_status_code,
            qbo_status_code=None,
            latency_ms=None,
            result="success",
            idempotent_reuse=True,
        )
        return QBOProxyResponse.model_validate({**cached_body, "idempotent_reuse": True})

    try:
        data, refreshed, latency_ms, qbo_status_code = await qbo_service.post(
            session,
            credential,
            entity=entity,
            resource=resource,
            payload=payload,
        )
    except QuickBooksApiError as exc:
        logging_utils.log_qbo_txn_finished(
            client_id=client_id_str,
            realm_id=credential.realm_id,
            environment=env,
            txn_type=txn_type,
            txn_id=txn_id,
            doc_number=doc_number,
            idempotency_key=idempotency_key,
            gateway_status_code=status.HTTP_502_BAD_GATEWAY,
            qbo_status_code=exc.status_code,
            latency_ms=None,
            result="failure",
            error_code=_classify_error(exc.status_code, exc),
            error_message=str(exc),
            qbo_error_details=_truncate(getattr(exc, "body", None)),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"QuickBooks API error: {exc}",
        ) from exc
    except QuickBooksOAuthError as exc:
        logging_utils.log_qbo_txn_finished(
            client_id=client_id_str,
            realm_id=credential.realm_id,
            environment=env,
            txn_type=txn_type,
            txn_id=txn_id,
            doc_number=doc_number,
            idempotency_key=idempotency_key,
            gateway_status_code=status.HTTP_502_BAD_GATEWAY,
            qbo_status_code=None,
            latency_ms=None,
            result="failure",
            error_code="qbo_oauth",
            error_message=str(exc),
            qbo_error_details=None,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    record.client_id = client_uuid
    response_model = QBOProxyResponse(
        client_id=client_id_str,
        realm_id=credential.realm_id,
        environment=env,
        fetched_at=datetime.now(timezone.utc),
        latency_ms=round(latency_ms, 2),
        data=data,
        refreshed=refreshed,
        idempotent_reuse=False,
    )
    await store_idempotent_response(session, record, jsonable_encoder(response_model))
    await session.commit()
    logging_utils.log_qbo_txn_finished(
        client_id=client_id_str,
        realm_id=credential.realm_id,
        environment=env,
        txn_type=txn_type,
        txn_id=txn_id,
        doc_number=doc_number,
        idempotency_key=idempotency_key,
        gateway_status_code=success_status_code,
        qbo_status_code=qbo_status_code,
        latency_ms=latency_ms,
        result="success",
        qbo_error_details=None,
    )
    return response_model


@router.post(
    "/{client_id}/salesreceipts",
    response_model=QBOProxyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_salesreceipt(
    client_id: str,
    payload: SalesReceiptCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auto_create: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    customer_ref = await resolver.resolve_customer(payload.customer, auto_create=auto_create)
    line_payloads, total_amount, line_descriptor = await _build_sales_lines(payload.lines, resolver)

    sales_payload: dict[str, Any] = {
        "TxnDate": payload.date.isoformat(),
        "CustomerRef": customer_ref,
        "Line": line_payloads,
    }
    if payload.private_note:
        sales_payload["PrivateNote"] = payload.private_note
    if payload.doc_number:
        sales_payload["DocNumber"] = payload.doc_number
    if payload.class_name:
        class_ref = await resolver.resolve_class(payload.class_name)
        sales_payload["ClassRef"] = class_ref

    fingerprint = build_fingerprint(
        credential.realm_id,
        "SalesReceipt",
        payload.date.isoformat(),
        total_amount,
        customer_ref["value"],
        line_descriptor or payload.private_note or "",
        payload.doc_number or "",
        payload.txn_id or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="salesreceipt:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="SalesReceipt",
        resource="salesreceipt",
        payload=sales_payload,
    )


@router.post(
    "/{client_id}/invoices",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create Invoice",
    description="Creates an Invoice (Accounts Receivable) in QuickBooks Online using the existing tokens and idempotency cache.",
)
async def create_invoice(
    client_id: str,
    payload: InvoiceCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auto_create: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    customer_ref = await resolver.resolve_customer(payload.customer, auto_create=auto_create)
    line_payloads, total_amount, _ = await _build_sales_lines(payload.lines, resolver)

    invoice_payload: dict[str, Any] = {
        "TxnDate": payload.date.isoformat(),
        "CustomerRef": customer_ref,
        "Line": line_payloads,
    }
    if payload.private_note:
        invoice_payload["PrivateNote"] = payload.private_note
    if payload.doc_number:
        invoice_payload["DocNumber"] = payload.doc_number
    if payload.class_name:
        class_ref = await resolver.resolve_class(payload.class_name)
        invoice_payload["ClassRef"] = class_ref

    fingerprint = build_fingerprint(
        credential.realm_id,
        "Invoice",
        payload.date.isoformat(),
        total_amount,
        customer_ref["value"],
        payload.doc_number or "",
        payload.txn_id or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="invoice:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="Invoice",
        resource="invoice",
        payload=invoice_payload,
    )


@router.post(
    "/{client_id}/expenses",
    response_model=QBOProxyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_expense(
    client_id: str,
    payload: ExpenseCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auto_create: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    allow_auto_create = auto_create or env == "sandbox"
    vendor_ref = await resolver.resolve_vendor(payload.vendor, auto_create=allow_auto_create)
    bank_ref = await resolver.ensure_account(
        payload.bank_account,
        account_type="Bank",
        account_sub_type="Checking",
        auto_create=allow_auto_create,
    )
    line_payloads, total_amount, line_descriptor = await _build_expense_lines(
        payload.lines,
        resolver,
        auto_create_accounts=allow_auto_create,
    )

    expense_payload: dict[str, Any] = {
        "TxnDate": payload.date.isoformat(),
        "EntityRef": vendor_ref,
        "Line": line_payloads,
        "AccountRef": bank_ref,
        "PaymentType": "Cash",
    }
    if payload.private_note:
        expense_payload["PrivateNote"] = payload.private_note
    if payload.doc_number:
        expense_payload["DocNumber"] = payload.doc_number

    fingerprint = build_fingerprint(
        credential.realm_id,
        "Purchase",
        payload.date.isoformat(),
        total_amount,
        vendor_ref["value"],
        line_descriptor or payload.private_note or "",
        payload.doc_number or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="expense:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="Purchase",
        resource="purchase",
        payload=expense_payload,
    )


@router.post(
    "/{client_id}/bills",
    response_model=QBOProxyResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    summary="Create Bill",
    description="Creates an Accounts Payable Bill with item or account based lines.",
)
async def create_bill(
    client_id: str,
    payload: BillCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    auto_create: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    environment: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> QBOProxyResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    client_uuid, env, credential = await _get_client_context(client_id, environment, session, settings)
    client_id_str = str(client_uuid)
    qbo_service = QuickBooksService(settings)
    resolver = QBOReferenceResolver(qbo_service, session, credential)
    vendor_ref = await resolver.resolve_vendor(payload.vendor, auto_create=auto_create)
    line_payloads, total_amount = await _build_bill_lines(payload.lines, resolver)

    bill_payload: dict[str, Any] = {
        "TxnDate": payload.date.isoformat(),
        "VendorRef": vendor_ref,
        "Line": line_payloads,
    }
    if payload.private_note:
        bill_payload["PrivateNote"] = payload.private_note
    if payload.doc_number:
        bill_payload["DocNumber"] = payload.doc_number
    if payload.class_name:
        class_ref = await resolver.resolve_class(payload.class_name)
        bill_payload["ClassRef"] = class_ref

    fingerprint = build_fingerprint(
        credential.realm_id,
        "Bill",
        payload.date.isoformat(),
        total_amount,
        vendor_ref["value"],
        payload.doc_number or "",
        payload.txn_id or "",
    )
    return await _execute_qbo_post(
        session=session,
        credential=credential,
        qbo_service=qbo_service,
        client_uuid=client_uuid,
        client_id_str=client_id_str,
        env=env,
        resource_type="bill:create",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(by_alias=True),
        fingerprint=fingerprint,
        entity="Bill",
        resource="bill",
        payload=bill_payload,
    )


async def _build_sales_lines(
    lines: Iterable[QBODocumentLine],
    resolver: QBOReferenceResolver,
) -> tuple[list[dict[str, Any]], Decimal, Optional[str]]:
    payload_lines: list[dict[str, Any]] = []
    total_amount = Decimal("0")
    first_description: Optional[str] = None
    for line in lines:
        total_amount += line.amount
        detail = {
            "Amount": float(line.amount),
            "DetailType": "SalesItemLineDetail",
        }
        try:
            item_ref = await resolver.resolve_item(line.account_or_item)
            sales_detail: dict[str, Any] = {"ItemRef": item_ref}
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            account_ref = await resolver.resolve_account(line.account_or_item)
            sales_detail = {"ItemAccountRef": account_ref}
        if line.class_name:
            class_ref = await resolver.resolve_class(line.class_name)
            sales_detail["ClassRef"] = class_ref
        detail["SalesItemLineDetail"] = sales_detail
        if line.description:
            detail["Description"] = line.description
            if first_description is None:
                first_description = line.description
        payload_lines.append(detail)
    return payload_lines, total_amount, first_description


async def _build_bill_lines(
    lines: Iterable[BillLine],
    resolver: QBOReferenceResolver,
) -> tuple[list[dict[str, Any]], Decimal]:
    payload_lines: list[dict[str, Any]] = []
    total_amount = Decimal("0")
    for line in lines:
        total_amount += line.amount
        try:
            item_ref = await resolver.resolve_item(line.account_or_item)
            detail_type = "ItemBasedExpenseLineDetail"
            content: dict[str, Any] = {"ItemRef": item_ref}
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            account_ref = await resolver.resolve_account(line.account_or_item)
            detail_type = "AccountBasedExpenseLineDetail"
            content = {"AccountRef": account_ref}
        if line.class_name:
            class_ref = await resolver.resolve_class(line.class_name)
            content["ClassRef"] = class_ref
        detail = {
            "Amount": float(line.amount),
            "DetailType": detail_type,
            detail_type: content,
        }
        if line.description:
            detail["Description"] = line.description
        payload_lines.append(detail)
    return payload_lines, total_amount


async def _build_deposit_lines(
    lines: Iterable[DepositLine],
    resolver: QBOReferenceResolver,
    *,
    auto_create_accounts: bool = False,
    auto_create_entities: bool = False,
) -> tuple[list[dict[str, Any]], Decimal, Optional[str]]:
    payload_lines: list[dict[str, Any]] = []
    total_amount = Decimal("0")
    first_description: Optional[str] = None
    for line in lines:
        total_amount += line.amount
        account_ref: dict[str, str]
        try:
            account_ref = await resolver.resolve_account(line.account_or_item)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            try:
                account_ref = await resolver.resolve_item_income_account(line.account_or_item)
            except HTTPException as item_exc:
                if (
                    item_exc.status_code == status.HTTP_404_NOT_FOUND
                    and auto_create_accounts
                ):
                    account_ref = await resolver.ensure_account(
                        line.account_or_item,
                        account_type="Income",
                        account_sub_type="SalesOfProductIncome",
                        auto_create=True,
                    )
                else:
                    raise
        detail: dict[str, Any] = {
            "Amount": float(line.amount),
            "DetailType": "DepositLineDetail",
            "DepositLineDetail": {
                "AccountRef": account_ref,
            },
        }
        if line.entity_type and line.entity_name:
            entity_ref = await resolver.resolve_entity_with_auto_create(
                line.entity_name,
                line.entity_type,
                auto_create=auto_create_entities,
            )
            detail["DepositLineDetail"]["Entity"] = entity_ref
        if line.class_name:
            class_ref = await resolver.resolve_class(line.class_name)
            detail["DepositLineDetail"]["ClassRef"] = class_ref
        if line.description:
            detail["Description"] = line.description
            if first_description is None:
                first_description = line.description
        payload_lines.append(detail)
    return payload_lines, total_amount, first_description


async def _build_payment_lines(
    lines: Iterable[PaymentLine],
    resolver: QBOReferenceResolver,
) -> tuple[list[dict[str, Any]], Decimal, str]:
    payload_lines: list[dict[str, Any]] = []
    total_amount = Decimal("0")
    doc_numbers: set[str] = set()
    for idx, line in enumerate(lines, start=1):
        total_amount += line.amount
        identifier = line.linked_doc_number or line.account_or_item
        txn_ref = await resolver.resolve_invoice_txn(identifier)
        ref_id = str(txn_ref["doc_number"] or txn_ref["value"])
        doc_numbers.add(ref_id)
        detail = {
            "Amount": float(line.amount),
            "LineNum": idx,
            "LinkedTxn": [
                {
                    "TxnId": txn_ref["value"],
                    "TxnType": "Invoice",
                }
            ],
        }
        if line.description:
            detail["Description"] = line.description
        payload_lines.append(detail)
    return payload_lines, total_amount, ",".join(sorted(doc_numbers))


async def _build_billpayment_lines(
    lines: Iterable[BillPaymentLine],
    resolver: QBOReferenceResolver,
) -> tuple[list[dict[str, Any]], Decimal, str]:
    payload_lines: list[dict[str, Any]] = []
    total_amount = Decimal("0")
    doc_numbers: set[str] = set()
    for line in lines:
        total_amount += line.amount
        identifier = line.linked_doc_number or line.account_or_item
        txn_ref = await resolver.resolve_bill_txn(identifier)
        ref_id = str(txn_ref["doc_number"] or txn_ref["value"])
        doc_numbers.add(ref_id)
        detail: dict[str, Any] = {
            "Amount": float(line.amount),
            "LinkedTxn": [
                {
                    "TxnId": txn_ref["value"],
                    "TxnType": "Bill",
                }
            ],
        }
        if line.description:
            detail["Description"] = line.description
        payload_lines.append(detail)
    return payload_lines, total_amount, ",".join(sorted(doc_numbers))


async def _build_expense_lines(
    lines: list[ExpenseLine],
    resolver: QBOReferenceResolver,
    *,
    auto_create_accounts: bool = False,
) -> tuple[list[dict[str, Any]], Decimal, Optional[str]]:
    payload_lines: list[dict[str, Any]] = []
    total_amount = Decimal("0")
    first_description: Optional[str] = None
    for line in lines:
        total_amount += line.amount
        try:
            account_ref = await resolver.resolve_account(line.expense_account)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND and auto_create_accounts:
                account_ref = await resolver.ensure_account(
                    line.expense_account,
                    account_type="Expense",
                    account_sub_type="OfficeGeneralAdministrativeExpenses",
                    auto_create=True,
                )
            else:
                raise
        detail = {
            "Amount": float(line.amount),
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": account_ref,
            },
        }
        if line.class_name:
            class_ref = await resolver.resolve_class(line.class_name)
            detail["AccountBasedExpenseLineDetail"]["ClassRef"] = class_ref
        if line.description:
            detail["Description"] = line.description
            if first_description is None:
                first_description = line.description
        payload_lines.append(detail)
    return payload_lines, total_amount, first_description


def _build_contact_payload(contact: CustomerCreate | VendorCreate) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "DisplayName": contact.display_name,
    }
    if contact.email:
        payload["PrimaryEmailAddr"] = {"Address": contact.email}
    if contact.phone:
        payload["PrimaryPhone"] = {"FreeFormNumber": contact.phone}
    if contact.address:
        addr = {
            "Line1": contact.address.line1,
        }
        if contact.address.line2:
            addr["Line2"] = contact.address.line2
        if contact.address.city:
            addr["City"] = contact.address.city
        if contact.address.state:
            addr["CountrySubDivisionCode"] = contact.address.state
        if contact.address.postal_code:
            addr["PostalCode"] = contact.address.postal_code
        if contact.address.country:
            addr["Country"] = contact.address.country
        payload["BillAddr"] = addr
    return payload


async def _get_client_context(
    client_id: str,
    environment: str | None,
    session: AsyncSession,
    settings: Settings,
) -> tuple[UUID, str, ClientCredentials]:
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
    return client_uuid, env, credential


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_date(value: date) -> str:
    return value.isoformat()


def _escape(value: str) -> str:
    return value.replace("'", "''")


def _compute_next_startposition(
    query_response: dict[str, Any],
    *,
    start_position: int,
    max_results: int,
    item_count: int,
) -> Optional[int]:
    if item_count == 0:
        return None
    total_count = query_response.get("totalCount")
    candidate = start_position + item_count
    if isinstance(total_count, int):
        return candidate if candidate <= total_count else None
    if item_count == max_results:
        return candidate
    return None
