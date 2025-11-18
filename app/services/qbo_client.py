from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.http import get_async_client, request_with_retry_and_backoff
from app.core.security import decrypt_refresh_token, encrypt_refresh_token
from app.db import repo
from app.db.models import ClientCredentials, Clients
from app.schemas.client import Environment


class QuickBooksOAuthError(RuntimeError):
    pass


class QuickBooksApiError(RuntimeError):
    pass


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    scopes: list[str]
    token_type: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


class QuickBooksService:
    AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
    TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    SANDBOX_API_BASE = "https://sandbox-quickbooks.api.intuit.com"
    PROD_API_BASE = "https://quickbooks.api.intuit.com"
    SCOPES = ["com.intuit.quickbooks.accounting"]
    MINOR_VERSION = "65"
    REFRESH_THRESHOLD = timedelta(minutes=5)

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.logger = logging.getLogger("app.services.qbo")

    def build_authorization_url(self, state: str, environment: Environment) -> str:
        params = {
            "client_id": self.settings.qbo_client_id,
            "redirect_uri": str(self.settings.qbo_redirect_uri),
            "response_type": "code",
            "scope": " ".join(self.SCOPES),
            "state": state,
        }
        url = httpx.URL(self.AUTH_URL, params=params)
        self.logger.info(
            "oauth_authorization_url_generated",
            extra={"environment": environment},
        )
        return str(url)

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        realm_id: str,
    ) -> TokenBundle:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": str(self.settings.qbo_redirect_uri),
        }
        response = await self._token_request(data)
        return self._parse_token_response(response, realm_id)

    async def refresh_tokens(self, *, refresh_token: str, realm_id: str) -> TokenBundle:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        response = await self._token_request(data)
        return self._parse_token_response(response, realm_id)

    async def upsert_credentials(
        self,
        session: AsyncSession,
        *,
        client: Clients,
        environment: Environment,
        realm_id: str,
        bundle: TokenBundle,
    ) -> ClientCredentials:
        credential = await repo.get_credential_optional(
            session,
            client_id=client.id,
            environment=environment,
        )
        encrypted_refresh = encrypt_refresh_token(self.settings.fernet_key, bundle.refresh_token)

        if credential is None:
            credential = ClientCredentials(
                client_id=client.id,
                realm_id=realm_id,
                environment=environment,
                refresh_token_enc=encrypted_refresh,
                access_token=bundle.access_token,
                access_expires_at=bundle.access_expires_at,
                refresh_expires_at=bundle.refresh_expires_at,
                scopes=bundle.scopes,
                refresh_counter=0,
            )
            await repo.save_credential(session, credential)
            self.logger.info(
                "credential_created",
                extra={
                    "client_id": str(client.id),
                    "realm_id": realm_id,
                    "environment": environment,
                },
            )
            return credential

        credential.realm_id = realm_id
        credential.refresh_token_enc = encrypted_refresh
        credential.access_token = bundle.access_token
        credential.access_expires_at = bundle.access_expires_at
        credential.refresh_expires_at = bundle.refresh_expires_at
        credential.scopes = bundle.scopes
        credential.refresh_counter = credential.refresh_counter if credential.refresh_counter else 0
        await repo.save_credential(session, credential)
        self.logger.info(
            "credential_updated",
            extra={
                "client_id": str(client.id),
                "realm_id": realm_id,
                "environment": environment,
            },
        )
        return credential

    async def ensure_valid_access_token(
        self,
        session: AsyncSession,
        credential: ClientCredentials,
    ) -> tuple[str, bool]:
        refreshed = False
        if credential.access_token is None or credential.access_expires_at is None:
            refreshed = await self._refresh_credential(session, credential)
        else:
            if credential.access_expires_at <= _now() + self.REFRESH_THRESHOLD:
                refreshed = await self._refresh_credential(session, credential)
        if not credential.access_token:
            raise QuickBooksApiError("Missing access token after refresh")
        return credential.access_token, refreshed

    async def fetch_company_info(
        self,
        session: AsyncSession,
        credential: ClientCredentials,
    ) -> tuple[dict, bool, float]:
        token, refreshed = await self.ensure_valid_access_token(session, credential)
        url = self._build_company_info_url(credential)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        latency_ms = 0.0
        async with get_async_client(self.settings) as client:
            start = perf_counter()
            response = await request_with_retry_and_backoff(
                client,
                method="GET",
                url=url,
                params={"minorversion": self.MINOR_VERSION},
                headers=headers.copy(),
                settings=self.settings,
            )
            latency_ms = (perf_counter() - start) * 1000

            if response.status_code == 401:
                self.logger.warning(
                    "qbo_unauthorized",
                    extra={
                        "client_id": str(credential.client_id),
                        "realm_id": credential.realm_id,
                        "environment": credential.environment,
                    },
                )
                refreshed = await self._refresh_credential(session, credential, force=True)
                headers["Authorization"] = f"Bearer {credential.access_token}"
                start = perf_counter()
                response = await request_with_retry_and_backoff(
                    client,
                    method="GET",
                    url=url,
                    params={"minorversion": self.MINOR_VERSION},
                    headers=headers.copy(),
                    settings=self.settings,
                )
                latency_ms = (perf_counter() - start) * 1000

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise QuickBooksApiError(f"QBO API error: {exc.response.status_code}") from exc

            payload = response.json()
            return payload, refreshed, latency_ms

    async def query(
        self,
        session: AsyncSession,
        credential: ClientCredentials,
        *,
        entity: str,
        select_sql: str,
        startposition: int | None = None,
        maxresults: int | None = None,
    ) -> tuple[dict, bool, float]:
        token, refreshed = await self.ensure_valid_access_token(session, credential)
        url = self._build_query_url(credential)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        statement = select_sql.strip()
        if startposition:
            statement = f"{statement} STARTPOSITION {startposition}"
        if maxresults:
            statement = f"{statement} MAXRESULTS {maxresults}"
        latency_ms = 0.0
        params = {
            "query": statement,
            "minorversion": self.MINOR_VERSION,
        }
        async with get_async_client(self.settings) as client:
            start = perf_counter()
            response = await request_with_retry_and_backoff(
                client,
                method="GET",
                url=url,
                params=params,
                headers=headers.copy(),
                settings=self.settings,
            )
            latency_ms = (perf_counter() - start) * 1000

            if response.status_code == 401:
                self.logger.warning(
                    "qbo_query_unauthorized",
                    extra={
                        "entity": entity,
                        "client_id": str(credential.client_id),
                        "realm_id": credential.realm_id,
                        "environment": credential.environment,
                    },
                )
                refreshed = await self._refresh_credential(session, credential, force=True)
                headers["Authorization"] = f"Bearer {credential.access_token}"
                start = perf_counter()
                response = await request_with_retry_and_backoff(
                    client,
                    method="GET",
                    url=url,
                    params=params,
                    headers=headers.copy(),
                    settings=self.settings,
                )
                latency_ms = (perf_counter() - start) * 1000

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text
                status_code = exc.response.status_code
                self.logger.error(
                    "qbo_query_failed",
                    extra={
                        "entity": entity,
                        "status": status_code,
                        "body": body,
                        "client_id": str(credential.client_id),
                        "realm_id": credential.realm_id,
                        "environment": credential.environment,
                    },
                )
                raise QuickBooksApiError(
                    f"QBO query error for {entity}: {status_code} {body}"
                ) from exc

            payload = response.json()
            return payload, refreshed, latency_ms

    async def query_accounts(
        self,
        session: AsyncSession,
        credential: ClientCredentials,
        *,
        select_sql: str,
        startposition: int | None = None,
        maxresults: int | None = None,
    ) -> tuple[dict, bool, float]:
        return await self.query(
            session,
            credential,
            entity="Account",
            select_sql=select_sql,
            startposition=startposition,
            maxresults=maxresults,
        )

    async def get_account_by_id(
        self,
        session: AsyncSession,
        credential: ClientCredentials,
        account_id: str,
    ) -> tuple[dict, bool, float]:
        escaped = self._escape(account_id)
        query = f"select * from Account where Id = '{escaped}'"
        return await self.query_accounts(
            session,
            credential,
            select_sql=query,
            startposition=1,
            maxresults=1,
        )

    async def post(
        self,
        session: AsyncSession,
        credential: ClientCredentials,
        *,
        entity: str,
        resource: str,
        payload: dict,
    ) -> tuple[dict, bool, float]:
        token, refreshed = await self.ensure_valid_access_token(session, credential)
        url = self._build_entity_url(credential, resource)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        latency_ms = 0.0
        async with get_async_client(self.settings) as client:
            start = perf_counter()
            response = await request_with_retry_and_backoff(
                client,
                method="POST",
                url=url,
                json=payload,
                params={"minorversion": self.MINOR_VERSION},
                headers=headers.copy(),
                settings=self.settings,
            )
            latency_ms = (perf_counter() - start) * 1000

            if response.status_code == 401:
                self.logger.warning(
                    "qbo_post_unauthorized",
                    extra={
                        "entity": entity,
                        "client_id": str(credential.client_id),
                        "realm_id": credential.realm_id,
                        "environment": credential.environment,
                    },
                )
                refreshed = await self._refresh_credential(session, credential, force=True)
                headers["Authorization"] = f"Bearer {credential.access_token}"
                start = perf_counter()
                response = await request_with_retry_and_backoff(
                    client,
                    method="POST",
                    url=url,
                    json=payload,
                    params={"minorversion": self.MINOR_VERSION},
                    headers=headers.copy(),
                    settings=self.settings,
                )
                latency_ms = (perf_counter() - start) * 1000

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text
                status_code = exc.response.status_code
                self.logger.error(
                    "qbo_post_failed",
                    extra={
                        "entity": entity,
                        "status": status_code,
                        "body": body,
                        "client_id": str(credential.client_id),
                        "realm_id": credential.realm_id,
                        "environment": credential.environment,
                    },
                )
                raise QuickBooksApiError(
                    f"QBO post error for {entity}: {status_code} {body}"
                ) from exc

            response_payload = response.json()
            return response_payload, refreshed, latency_ms

    async def rotate_credential(
        self,
        session: AsyncSession,
        credential: ClientCredentials,
    ) -> None:
        await self._refresh_credential(session, credential, force=True)

    def _build_company_info_url(self, credential: ClientCredentials) -> str:
        base = self._build_company_base_url(credential)
        return f"{base}/companyinfo/{credential.realm_id}"

    def _build_query_url(self, credential: ClientCredentials) -> str:
        base = self._build_company_base_url(credential)
        return f"{base}/query"

    def _build_entity_url(self, credential: ClientCredentials, resource: str) -> str:
        base = self._build_company_base_url(credential)
        return f"{base}/{resource}"

    def _build_company_base_url(self, credential: ClientCredentials) -> str:
        base = (
            self.SANDBOX_API_BASE
            if credential.environment == "sandbox"
            else self.PROD_API_BASE
        )
        return f"{base}/v3/company/{credential.realm_id}"

    async def _refresh_credential(
        self,
        session: AsyncSession,
        credential: ClientCredentials,
        *,
        force: bool = False,
    ) -> bool:
        refresh_token = decrypt_refresh_token(
            self.settings.fernet_key,
            credential.refresh_token_enc,
        )
        bundle = await self.refresh_tokens(refresh_token=refresh_token, realm_id=credential.realm_id)
        credential.access_token = bundle.access_token
        credential.access_expires_at = bundle.access_expires_at
        credential.refresh_expires_at = bundle.refresh_expires_at
        credential.refresh_token_enc = encrypt_refresh_token(
            self.settings.fernet_key, bundle.refresh_token
        )
        credential.scopes = bundle.scopes
        credential.refresh_counter = (credential.refresh_counter or 0) + 1
        await repo.save_credential(session, credential)
        self.logger.info(
            "credential_refreshed",
            extra={
                "client_id": str(credential.client_id),
                "realm_id": credential.realm_id,
                "environment": credential.environment,
                "force": force,
            },
        )
        return True

    async def _token_request(self, data: dict[str, str]) -> dict:
        auth_header = self._basic_auth_header()
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        async with get_async_client(self.settings) as client:
            response = await request_with_retry_and_backoff(
                client,
                "POST",
                self.TOKEN_URL,
                data=data,
                headers=headers,
                settings=self.settings,
            )
        if response.status_code >= 400:
            self.logger.error(
                "oauth_token_error",
                extra={
                    "status": response.status_code,
                    "body": response.text,
                },
            )
            raise QuickBooksOAuthError(
                f"Failed to obtain tokens from Intuit (status {response.status_code}): {response.text}"
            )
        return response.json()

    def _parse_token_response(self, payload: dict, realm_id: str) -> TokenBundle:
        now = _now()
        try:
            access_expires_in = int(payload["expires_in"])
            refresh_expires_in = int(payload.get("x_refresh_token_expires_in", 0))
            access_token = payload["access_token"]
            refresh_token = payload["refresh_token"]
            scope_raw = payload.get("scope", "")
            token_type = payload.get("token_type", "Bearer")
        except KeyError as exc:
            raise QuickBooksOAuthError("Incomplete token response") from exc

        scopes = [scope for scope in scope_raw.split() if scope]

        bundle = TokenBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=now + timedelta(seconds=access_expires_in),
            refresh_expires_at=now + timedelta(seconds=refresh_expires_in),
            scopes=scopes,
            token_type=token_type,
        )

        self.logger.info(
            "token_bundle_parsed",
            extra={
                "realm_id": realm_id,
                "access_expires_at": bundle.access_expires_at.isoformat(),
                "refresh_expires_at": bundle.refresh_expires_at.isoformat(),
            },
        )
        return bundle

    def _basic_auth_header(self) -> str:
        credentials = f"{self.settings.qbo_client_id}:{self.settings.qbo_client_secret}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        return f"Basic {encoded}"

    def _escape(self, value: str) -> str:
        return value.replace("'", "''")
