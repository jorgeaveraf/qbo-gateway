from __future__ import annotations

from collections import defaultdict
import json
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_ctx
from app.db.models import ClientCredentials
from app.services.qbo_client import QuickBooksApiError, QuickBooksOAuthError, QuickBooksService


class QBOReferenceResolver:
    def __init__(
        self,
        qbo_service: QuickBooksService,
        session: AsyncSession,
        credential: ClientCredentials,
    ) -> None:
        self.qbo_service = qbo_service
        self.session = session
        self.credential = credential
        self._cache: dict[str, Dict[str, dict[str, str]]] = defaultdict(dict)
        self._records: dict[str, Dict[str, dict[str, Any]]] = defaultdict(dict)

    async def resolve_customer(self, identifier: str, *, auto_create: bool = False) -> dict[str, str]:
        reference, _ = await self._resolve_entity(
            "Customer",
            identifier,
            name_field="DisplayName",
            case_insensitive=False,
        )
        if reference:
            return reference
        if auto_create:
            payload = {"DisplayName": identifier}
            data, _, _, _ = await self.qbo_service.post(
                self.session,
                self.credential,
                entity="Customer",
                resource="customer",
                payload=payload,
            )
            created = self._extract_entity(data, "Customer")
            if created is None:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Unable to auto-create customer in QuickBooks",
                )
            reference = self._build_reference(created)
            self._store_cache("Customer", identifier, reference, created)
            return reference
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Customer '{identifier}' not found",
        )

    async def resolve_vendor(self, identifier: str, *, auto_create: bool = False) -> dict[str, str]:
        reference, _ = await self._resolve_entity(
            "Vendor",
            identifier,
            name_field="DisplayName",
            case_insensitive=False,
        )
        if reference:
            return reference
        if auto_create:
            payload = {"DisplayName": identifier}
            data, _, _, _ = await self.qbo_service.post(
                self.session,
                self.credential,
                entity="Vendor",
                resource="vendor",
                payload=payload,
            )
            created = self._extract_entity(data, "Vendor")
            if created is None:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Unable to auto-create vendor in QuickBooks",
                )
            reference = self._build_reference(created)
            self._store_cache("Vendor", identifier, reference, created)
            return reference
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Vendor '{identifier}' not found",
        )

    async def resolve_account(
        self,
        identifier: str,
        *,
        account_type: Optional[str] = None,
    ) -> dict[str, str]:
        reference, _ = await self._resolve_account_reference(
            identifier,
            account_type=account_type,
        )
        if reference:
            return reference
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Account '{identifier}' not found",
        )

    async def ensure_account(
        self,
        identifier: str,
        *,
        account_type: Optional[str] = None,
        account_sub_type: Optional[str] = None,
        auto_create: bool = False,
    ) -> dict[str, str]:
        reference, _ = await self._resolve_account_reference(
            identifier,
            account_type=account_type,
        )
        if reference:
            return reference
        if not auto_create:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Account '{identifier}' not found",
            )
        return await self._create_account(
            identifier,
            account_type=account_type,
            account_sub_type=account_sub_type,
        )

    async def resolve_entity(self, name: str, entity_type: str) -> dict[str, str]:
        mapping = {
            "Customer": ("Customer", "DisplayName"),
            "Vendor": ("Vendor", "DisplayName"),
            "Employee": ("Employee", "DisplayName"),
            "Other": ("OtherName", "DisplayName"),
        }
        qbo_entity, name_field = mapping.get(entity_type, (None, None))
        if qbo_entity is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid entity_type",
            )
        record = await self._resolve_record(
            qbo_entity,
            name,
            name_field=name_field,
            allow_numeric_name_match=True,
            case_insensitive=False,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown {entity_type}: {name}",
            )
        reference: dict[str, str] = {
            "type": entity_type,
            "value": str(record.get("Id")),
        }
        display_name = record.get("DisplayName") or record.get(name_field)
        if display_name:
            reference["name"] = str(display_name)
        self._store_cache(qbo_entity, name, {"value": reference["value"], "name": reference.get("name", "")}, record)
        return reference

    async def resolve_entity_with_auto_create(
        self,
        name: str,
        entity_type: str,
        *,
        auto_create: bool = False,
    ) -> dict[str, str]:
        if entity_type == "Customer":
            reference = await self.resolve_customer(name, auto_create=auto_create)
            return {"type": entity_type, **reference}
        if entity_type == "Vendor":
            reference = await self.resolve_vendor(name, auto_create=auto_create)
            return {"type": entity_type, **reference}
        return await self.resolve_entity(name, entity_type)

    async def get_account(self, account_ref: str | int) -> tuple[dict[str, Any], bool, float]:
        identifier = str(account_ref).strip()
        cache_key = self._build_cache_key(identifier)
        if cache_key in self._records["Account"]:
            return self._records["Account"][cache_key], False, 0.0

        refreshed = False
        latency_ms = 0.0
        record: Optional[dict[str, Any]] = None

        if identifier.isdigit():
            payload, refreshed, latency_ms = await self.qbo_service.get_account_by_id(
                self.session, self.credential, account_id=identifier
            )
            record = self._extract_entity(payload, "Account")

        if record is None:
            record, refreshed, latency_ms = await self._resolve_record(
                "Account",
                identifier,
                name_field="FullyQualifiedName",
                allow_numeric_name_match=True,
                with_metadata=True,
                case_insensitive=False,
            )

        if record is None:
            record, refreshed, latency_ms = await self._resolve_record(
                "Account",
                identifier,
                name_field="Name",
                allow_numeric_name_match=True,
                with_metadata=True,
                case_insensitive=False,
            )

        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Account '{identifier}' not found",
            )

        reference = self._build_reference(record, name_field="Name")
        self._store_cache("Account", identifier, reference, record)
        return record, refreshed, latency_ms

    async def resolve_item(self, identifier: str) -> dict[str, str]:
        # Avoid UPPER() in queries (QBO rejects it for Item); match using FullyQualifiedName case-sensitively.
        reference, _ = await self._resolve_entity(
            "Item",
            identifier,
            name_field="FullyQualifiedName",
            case_insensitive=False,
        )
        if reference:
            return reference
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{identifier}' not found",
        )

    async def resolve_class(self, identifier: str) -> dict[str, str]:
        # QuickBooks rejects queries with UPPER() for Class, so force case-sensitive lookup
        # to avoid `QueryParserError` and still match fully qualified names.
        reference, _ = await self._resolve_entity(
            "Class",
            identifier,
            name_field="FullyQualifiedName",
            case_insensitive=False,
        )
        if reference:
            return reference
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Class '{identifier}' not found",
        )

    async def resolve_account_payload(self, identifier: str) -> dict[str, Any]:
        record, _, _ = await self.get_account(identifier)
        return record

    async def resolve_invoice_txn(self, identifier: str) -> dict[str, str]:
        record = await self._resolve_record(
            "Invoice",
            identifier,
            name_field="DocNumber",
            allow_numeric_name_match=True,
            case_insensitive=False,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Invoice '{identifier}' not found",
            )
        return {
            "value": str(record.get("Id")),
            "doc_number": str(record.get("DocNumber") or record.get("Id")),
        }

    async def resolve_bill_txn(self, identifier: str) -> dict[str, str]:
        record = await self._resolve_record(
            "Bill",
            identifier,
            name_field="DocNumber",
            allow_numeric_name_match=True,
            case_insensitive=False,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Bill '{identifier}' not found",
            )
        return {
            "value": str(record.get("Id")),
            "doc_number": str(record.get("DocNumber") or record.get("Id")),
        }

    async def resolve_item_income_account(self, identifier: str) -> dict[str, str]:
        # QuickBooks rejects UPPER() for Item queries; keep case-sensitive lookup.
        record = await self._resolve_record(
            "Item",
            identifier,
            name_field="Name",
            case_insensitive=False,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Item '{identifier}' not found",
            )
        account_ref = record.get("IncomeAccountRef")
        if not account_ref:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Item '{identifier}' is missing an income account",
            )
        return account_ref

    async def _resolve_entity(
        self,
        entity: str,
        identifier: str,
        *,
        name_field: str,
        extra_filters: Optional[list[str]] = None,
        case_insensitive: bool = True,
    ) -> tuple[Optional[dict[str, str]], Optional[dict[str, Any]]]:
        cache_key = self._build_cache_key(identifier)
        if cache_key in self._cache[entity]:
            return self._cache[entity][cache_key], self._records[entity].get(cache_key)

        record = await self._resolve_record(
            entity,
            identifier,
            name_field=name_field,
            extra_filters=extra_filters,
            case_insensitive=case_insensitive,
        )
        if record is None:
            return None, None
        reference = self._build_reference(record, name_field=name_field)
        self._store_cache(entity, identifier, reference, record)
        return reference, record

    async def _resolve_record(
        self,
        entity: str,
        identifier: str,
        *,
        name_field: str,
        extra_filters: Optional[list[str]] = None,
        allow_numeric_name_match: bool = False,
        with_metadata: bool = False,
        case_insensitive: bool = True,
    ) -> Optional[dict[str, Any]] | tuple[Optional[dict[str, Any]], bool, float]:
        cache_key = self._build_cache_key(identifier)
        if cache_key in self._records[entity]:
            cached = self._records[entity][cache_key]
            return (cached, False, 0.0) if with_metadata else cached

        normalized = identifier.strip()
        filters = list(extra_filters or [])

        # When numeric identifiers are allowed to match either Id or DocNumber, try Id first,
        # then fall back to name_field to avoid OR clauses that QuickBooks rejects.
        if normalized.isdigit() and allow_numeric_name_match:
            escaped = self._escape(normalized)
            candidate_filters = [f"Id = '{escaped}'"]
            if case_insensitive:
                candidate_filters.append(f"UPPER({name_field}) = '{escaped.upper()}'")
            else:
                candidate_filters.append(f"{name_field} = '{escaped}'")
            for clause in candidate_filters:
                where_clause = " AND ".join(filters + [clause])
                query = f"select * from {entity} where {where_clause}"
                try:
                    data, refreshed, latency_ms = await self.qbo_service.query(
                        self.session,
                        self.credential,
                        entity=entity,
                        select_sql=query,
                        startposition=1,
                        maxresults=1,
                    )
                except QuickBooksOAuthError:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="QuickBooks credentials are invalid or expired",
                    )
                except QuickBooksApiError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="QuickBooks API error",
                    ) from exc
                record = self._extract_entity(data, entity)
                if record is not None:
                    self._records[entity][cache_key] = record
                    return (record, refreshed, latency_ms) if with_metadata else record
            return (None, False, 0.0) if with_metadata else None

        where_clause = self._build_where_clause(
            identifier,
            name_field,
            filters,
            allow_numeric_name_match=allow_numeric_name_match,
            case_insensitive=case_insensitive,
        )
        query = f"select * from {entity} where {where_clause}"
        try:
            data, refreshed, latency_ms = await self.qbo_service.query(
                self.session,
                self.credential,
                entity=entity,
                select_sql=query,
                startposition=1,
                maxresults=1,
            )
        except QuickBooksOAuthError:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="QuickBooks credentials are invalid or expired",
            )
        except QuickBooksApiError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="QuickBooks API error",
            ) from exc
        record = self._extract_entity(data, entity)
        if record is not None:
            self._records[entity][cache_key] = record
        if with_metadata:
            return record, refreshed, latency_ms
        return record

    def _build_where_clause(
        self,
        identifier: str,
        name_field: str,
        extra_filters: Optional[list[str]],
        *,
        allow_numeric_name_match: bool = False,
        case_insensitive: bool = True,
    ) -> str:
        normalized = identifier.strip()
        filters = list(extra_filters or [])
        if normalized.isdigit():
            escaped = self._escape(normalized)
            clauses = [f"Id = '{escaped}'"]
            if allow_numeric_name_match:
                if case_insensitive:
                    clauses.append(f"UPPER({name_field}) = '{escaped.upper()}'")
                else:
                    clauses.append(f"{name_field} = '{escaped}'")
            filters.append(" OR ".join(clauses))
        else:
            if case_insensitive:
                filters.append(f"UPPER({name_field}) = '{self._escape(normalized.upper())}'")
            else:
                filters.append(f"{name_field} = '{self._escape(normalized)}'")
        return " AND ".join(filters)

    def _extract_entity(self, payload: dict[str, Any], entity: str) -> Optional[dict[str, Any]]:
        query_response = payload.get("QueryResponse")
        if query_response is None:
            return payload.get(entity)
        items = query_response.get(entity)
        if not items:
            return None
        if isinstance(items, list):
            return items[0]
        return items

    def _build_reference(self, entity_payload: dict[str, Any], name_field: str | None = None) -> dict[str, str]:
        value = entity_payload.get("Id")
        if value is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Malformed QuickBooks entity payload",
            )
        name = (
            entity_payload.get("DisplayName")
            if "DisplayName" in entity_payload
            else entity_payload.get("Name")
        )
        if name_field:
            name = entity_payload.get(name_field) or name
        reference: dict[str, str] = {"value": str(value)}
        if name:
            reference["name"] = str(name)
        return reference

    def _store_cache(
        self,
        entity: str,
        identifier: str,
        reference: dict[str, str],
        record: Optional[dict[str, Any]] = None,
    ) -> None:
        cache_key = self._build_cache_key(identifier)
        self._cache[entity][cache_key] = reference
        if record is not None:
            self._records[entity][cache_key] = record

    def _build_cache_key(self, identifier: str) -> str:
        normalized = identifier.strip()
        if normalized.isdigit():
            return f"id:{normalized}"
        return f"name:{normalized.lower()}"

    def _escape(self, value: str) -> str:
        return value.replace("'", "''")

    async def _resolve_account_reference(
        self,
        identifier: str,
        *,
        account_type: Optional[str],
    ) -> tuple[Optional[dict[str, str]], Optional[dict[str, Any]]]:
        record = await self._resolve_account_with_retries(
            identifier,
            account_type=account_type,
            allow_account_type_relaxation=True,
        )
        if record is None:
            return None, None
        reference = self._build_reference(record, name_field="Name")
        self._store_cache("Account", identifier, reference, record)
        return reference, record

    async def _resolve_account_with_retries(
        self,
        identifier: str,
        *,
        account_type: Optional[str],
        allow_account_type_relaxation: bool,
    ) -> Optional[dict[str, Any]]:
        expected_account_type = account_type
        candidates: list[Optional[str]] = [account_type] if account_type is not None else [None]
        if account_type is not None and allow_account_type_relaxation and None not in candidates:
            candidates.append(None)
        for candidate in candidates:
            record = await self._attempt_account_resolution(
                identifier,
                account_type=candidate,
            )
            if record:
                self._log_account_type_mismatch(
                    identifier=identifier,
                    expected_account_type=expected_account_type,
                    actual_account_type=record.get("AccountType"),
                )
                return record
        return None

    async def _attempt_account_resolution(
        self,
        identifier: str,
        *,
        account_type: Optional[str],
    ) -> Optional[dict[str, Any]]:
        normalized = identifier.strip()
        is_fully_qualified = ":" in normalized

        if is_fully_qualified:
            record = await self._query_account(
                identifier=normalized,
                name_field="FullyQualifiedName",
                account_type=None,
                case_insensitive=False,
            )
            if record:
                return record
            leaf = normalized.rsplit(":", 1)[-1].strip()
            record = await self._query_account(
                identifier=leaf,
                name_field="Name",
                account_type=account_type,
                case_insensitive=False,
            )
            return record

        record = await self._query_account(
            identifier=normalized,
            name_field="Name",
            account_type=account_type,
            case_insensitive=False,
        )
        return record

    async def _query_account(
        self,
        *,
        identifier: str,
        name_field: str,
        account_type: Optional[str],
        case_insensitive: bool,
    ) -> Optional[dict[str, Any]]:
        filters: list[str] = []
        if account_type:
            filters.append(f"AccountType = '{self._escape(account_type)}'")
        return await self._resolve_record(
            "Account",
            identifier,
            name_field=name_field,
            extra_filters=filters,
            case_insensitive=case_insensitive,
        )

    def _log_account_type_mismatch(
        self,
        *,
        identifier: str,
        expected_account_type: Optional[str],
        actual_account_type: Optional[str],
    ) -> None:
        if expected_account_type is None or actual_account_type is None:
            return
        if expected_account_type.strip().lower() == actual_account_type.strip().lower():
            return
        self.qbo_service.logger.warning(
            "account_type_mismatch",
            extra={
                "identifier": identifier,
                "expected_account_type": expected_account_type,
                "actual_account_type": actual_account_type,
                "request_id": request_id_ctx.get(),
                "realm_id": self.credential.realm_id,
            },
        )

    def _extract_qbo_error_code(self, body: Optional[str]) -> Optional[str]:
        if not body:
            return None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None
        fault = payload.get("Fault", {})
        errors = fault.get("Error") or []
        if isinstance(errors, dict):
            errors = [errors]
        for error in errors:
            code = error.get("code")
            if code:
                return str(code)
        return None

    async def _recover_from_duplicate_account_error(
        self,
        exc: QuickBooksApiError,
        *,
        original_identifier: str,
        payload_name: str,
        account_type: Optional[str],
    ) -> Optional[dict[str, str]]:
        error_code = self._extract_qbo_error_code(exc.body)
        if exc.status_code != 400 or error_code != "6240":
            return None

        self.qbo_service.logger.warning(
            "account_duplicate_detected",
            extra={
                "identifier": original_identifier,
                "payload_name": payload_name,
                "account_type": account_type,
                "request_id": request_id_ctx.get(),
                "realm_id": self.credential.realm_id,
            },
        )

        record = await self._resolve_account_with_retries(
            original_identifier,
            account_type=None,
            allow_account_type_relaxation=False,
        )
        if record is None and payload_name != original_identifier:
            record = await self._resolve_account_with_retries(
                payload_name,
                account_type=None,
                allow_account_type_relaxation=False,
            )
        if record is None:
            self.qbo_service.logger.error(
                "account_duplicate_recovery_failed",
                extra={
                    "identifier": original_identifier,
                    "payload_name": payload_name,
                    "account_type": account_type,
                    "request_id": request_id_ctx.get(),
                    "realm_id": self.credential.realm_id,
                    "qbo_error_code": error_code,
                },
            )
            return None

        reference = self._build_reference(record, name_field="Name")
        self._store_cache("Account", original_identifier, reference, record)
        if payload_name != original_identifier:
            self._store_cache("Account", payload_name, reference, record)
        self.qbo_service.logger.info(
            "account_duplicate_reused",
            extra={
                "identifier": original_identifier,
                "payload_name": payload_name,
                "account_type": account_type,
                "reused_account_id": record.get("Id"),
                "request_id": request_id_ctx.get(),
                "realm_id": self.credential.realm_id,
            },
        )
        return reference

    async def _create_account(
        self,
        name: str,
        *,
        account_type: Optional[str],
        account_sub_type: Optional[str] = None,
    ) -> dict[str, str]:
        final_name, parent_identifier = self._sanitize_account_name(name)
        payload: dict[str, Any] = {"Name": final_name}
        if account_type:
            payload["AccountType"] = account_type
        if account_sub_type:
            payload["AccountSubType"] = account_sub_type
        if parent_identifier:
            try:
                parent_ref = await self.resolve_account(parent_identifier)
                payload["ParentRef"] = parent_ref
            except HTTPException as exc:
                if exc.status_code != status.HTTP_404_NOT_FOUND:
                    raise
        self.qbo_service.logger.info(
            "account_create_attempt",
            extra={
                "original_identifier": name,
                "payload_name": final_name,
                "parent_identifier": parent_identifier,
                "account_type": account_type,
                "account_sub_type": account_sub_type,
                "request_id": request_id_ctx.get(),
                "realm_id": self.credential.realm_id,
            },
        )
        try:
            data, _, _, _ = await self.qbo_service.post(
                self.session,
                self.credential,
                entity="Account",
                resource="account",
                payload=payload,
            )
        except QuickBooksOAuthError:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="QuickBooks credentials are invalid or expired",
            )
        except QuickBooksApiError as exc:
            reused = await self._recover_from_duplicate_account_error(
                exc,
                original_identifier=name,
                payload_name=final_name,
                account_type=account_type,
            )
            if reused is not None:
                return reused
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="QuickBooks API error",
            ) from exc
        account = self._extract_entity(data, "Account")
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to auto-create account in QuickBooks",
            )
        reference = self._build_reference(account, name_field="Name")
        self._store_cache("Account", name, reference, account)
        return reference

    def _sanitize_account_name(self, name: str) -> tuple[str, Optional[str]]:
        normalized = name.strip()
        parent_identifier: Optional[str] = None
        leaf = normalized
        if ":" in normalized:
            parent_identifier, leaf = normalized.rsplit(":", 1)
            parent_identifier = parent_identifier.strip()
            leaf = leaf.strip()
        cleaned_leaf = self._strip_control_characters(leaf)
        final_name = cleaned_leaf or "Auto Account"
        return final_name, parent_identifier

    def _strip_control_characters(self, value: str) -> str:
        return "".join(ch for ch in value if ch not in ("\r", "\n", "\t"))
