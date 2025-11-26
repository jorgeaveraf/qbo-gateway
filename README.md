# QBO Gateway Service

HTTP microservice built with **FastAPI** that manages the **QuickBooks Online (Intuit) OAuth 2.0 lifecycle** with secure token rotation. It exposes endpoints for onboarding, client management, and an initial proxy operation (`companyinfo`).  
The service is designed to be consumed by other components (e.g., Airflow) via HTTP and packaged in Docker containers.

## Table of Contents
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Environment Variables](#environment-variables)
- [Running in Development (uv)](#running-in-development-uv)
- [Running with Docker Compose](#running-with-docker-compose)
- [Database Migrations](#database-migrations)
- [QuickBooks OAuth Flow](#quickbooks-oauth-flow)
- [API and Security](#api-and-security)
- [Postman Collection](#postman-collection)
- [Observability and Logging](#observability-and-logging)
- [Reference Resolution & Auto-Create (Design Notes)](#reference-resolution--auto-create-design-notes)
- [Testing Plan](#testing-plan)
- [Suggested Next Steps](#suggested-next-steps)

## Architecture
- **FastAPI + Uvicorn** for the HTTP server.  
- **SQLAlchemy 2.x** (async) and **Alembic** for persistence (PostgreSQL recommended; SQLite available for local tests).  
- **httpx + tenacity** to call Intuit endpoints with retries and backoff.  
- **cryptography (Fernet)** for encrypting `refresh_token` at rest.  
- **JSON logging** with request ID, client ID, and realmId for traceability.  
- **Idempotency keys** on `POST` requests to prevent duplicates.  
- **Multi-stage Dockerfile** and `docker-compose.yml` for a reproducible environment.

```
qbo-gateway/
├─ app/
│  ├─ api/               # FastAPI routers (auth, clients, qbo)
│  ├─ core/              # Config, logging, security, HTTP client
│  ├─ db/                # Models, async session, repository
│  ├─ services/          # QBO integrations (tokens, proxy)
│  ├─ schemas/           # Pydantic I/O models
│  └─ utils/             # Idempotency, hashing, validations
├─ alembic/              # Migration scripts
├─ Dockerfile
├─ docker-compose.yml
├─ postman/              # Postman collection
├─ .env.example
└─ README.md
```

## Prerequisites
- Python 3.11.x  
- [uv](https://github.com/astral-sh/uv) (fast dependency manager). Install via `pip install uv`.  
- Docker and Docker Compose (for deployment and dev/prod parity).  
- QuickBooks Online Sandbox app (Intuit Developer) with `client_id`, `client_secret`, and redirect URL configured.  

> **Windows:** Use WSL2 (Ubuntu) to match the production environment and simplify Docker Desktop usage.

## Environment Variables
1. Copy `.env.example` to `.env`.
2. Set the required values:
   - `API_KEY`: API key required by consumers in `X-API-Key`.
   - `FERNET_KEY`: Base64 32-byte key. Generate one with:
     ```bash
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```
   - `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REDIRECT_URI`: obtained from Intuit Developer portal.
   - `DATABASE_URL`: ideally `postgresql+psycopg://user:pass@host:5432/db`.
3. Adjust timeouts and retry policies as needed (`HTTP_TIMEOUT_SECONDS`, `RETRY_MAX_*`).

## Running in Development (uv)
```bash
# Optional virtual environment
python -m venv .venv
source .venv/bin/activate  # .venv\Scripts\activate on Windows

# Install dependencies with uv
uv pip install --upgrade pip
uv pip install --system .

# Start the server
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- OpenAPI docs: `http://localhost:8000/docs` (Swagger) or `http://localhost:8000/openapi.json`  
- Health check: `GET http://localhost:8000/health`

## Running with Docker Compose
```bash
cp .env.example .env
docker compose up --build
```

Services:
- `api`: FastAPI app with autoreload.
- `db`: PostgreSQL 16 with data persisted in the `pgdata` volume.

To stop: `docker compose down`. Add `-v` to remove the local volume.

## Database Migrations
```bash
# Apply migrations
uv run alembic upgrade head

# Create a new migration (example)
uv run alembic revision -m "feature xyz"
uv run alembic upgrade head
```

Alembic reads `DATABASE_URL` from your `.env`.  
For Docker environments, run commands inside the `api` container.

## QuickBooks OAuth Flow
1. Register redirect URL in Intuit Developer: `https://<host>/auth/callback`.
2. Create an internal client via `POST /clients` (send `Idempotency-Key`).
3. Start onboarding:
   ```
   GET /auth/connect?client_id=<UUID>&env=sandbox
   or
   curl.exe -i -H "X-API-Key: X-API-Key" "http://localhost:8000/auth/connect?client_id=clientId`&env=sandbox"    
   ```
   Redirects to Intuit for authorization (`com.intuit.quickbooks.accounting`).
4. Intuit calls the callback with `code`, `realmId`, and `state`.
5. The service exchanges the `code`, securely stores the encrypted `refresh_token`, logs `access_token` and expiration.
6. Endpoints `/clients/{id}/credentials` and `/qbo/{id}/companyinfo` automatically refresh tokens as needed.

### Security
- All routes except `/health`, `/docs`, `/openapi.json` require the `X-API-Key` header.  
- `refresh_token` is stored encrypted with Fernet and never logged.  
- Idempotency supported in `POST /clients`, `POST /qbo/{id}/salesreceipts`, and `POST /qbo/{id}/expenses` via the `Idempotency-Key` header.

## API and Security

| Route | Description | Auth |
|-------|--------------|------|
| `GET /auth/connect` | Redirects to Intuit login for a registered client. | `X-API-Key` |
| `GET /auth/callback` | Exchanges tokens and persists credentials. | `X-API-Key` |
| `POST /clients` | Creates client (idempotent). | `X-API-Key` |
| `GET /clients` | Lists clients (`summary=true` adds credential metadata, optional `env`). | `X-API-Key` |
| `GET /clients/{id}` | Client details + credentials. | `X-API-Key` |
| `PATCH /clients/{id}` | Updates client data. | `X-API-Key` |
| `DELETE /clients/{id}` | Deletes client and associated resources. | `X-API-Key` |
| `GET /clients/{id}/credentials` | Shows expiration info (not tokens). | `X-API-Key` |
| `POST /clients/{id}/credentials/rotate` | Forces token refresh. | `X-API-Key` |
| `GET /qbo/{id}/companyinfo` | Proxy to Intuit with auto-refresh. | `X-API-Key` |
| `GET /qbo/{id}/customers` | Paginated Customer list (filters + `next_startposition`). | `X-API-Key` |
| `GET /qbo/{id}/vendors` | Paginated Vendor list. | `X-API-Key` |
| `GET /qbo/{id}/items` | Paginated Item list. | `X-API-Key` |
| `GET /qbo/{id}/accounts` | Paginated Chart of Accounts with filters (`updated_since`, `account_type`, `classification`, `active`). | `X-API-Key` |
| `GET /qbo/{id}/accounts/{account_id}` | Account detail by QBO Id, Name, or FullyQualifiedName. | `X-API-Key` |
| `GET /qbo/{id}/invoices` | Paginated Invoice list with filter support. | `X-API-Key` |
| `GET /qbo/{id}/payments` | Paginated Payment list. | `X-API-Key` |
| `GET /qbo/{id}/salesreceipts` | Paginated SalesReceipt list. | `X-API-Key` |
| `GET /qbo/{id}/expenses` | Paginated cash `Purchase` list (AP). | `X-API-Key` |
| `GET /qbo/{id}/bills` | Paginated Bill list. | `X-API-Key` |
| `GET /qbo/{id}/billpayments` | Paginated BillPayment list. | `X-API-Key` |
| `GET /qbo/{id}/deposits` | Paginated Deposit list. | `X-API-Key` |
| `POST /qbo/{id}/salesreceipts` | Creates cash SalesReceipt (AR) with idempotency. | `X-API-Key` |
| `POST /qbo/{id}/invoices` | Creates AR Invoice with account/item-based lines. | `X-API-Key` |
| `POST /qbo/{id}/expenses` | Creates cash Expense (`Purchase`) with idempotency. | `X-API-Key` |
| `POST /qbo/{id}/bills` | Creates AP Bills with item/account lines. | `X-API-Key` |
| `POST /qbo/{id}/deposits` | Creates bank Deposits grouped by source lines. | `X-API-Key` |
| `POST /qbo/{id}/payments` | Applies customer Payments to invoices. | `X-API-Key` |
| `POST /qbo/{id}/billpayments` | Applies BillPayments to outstanding bills. | `X-API-Key` |
| `POST /qbo/{id}/customers` | Creates Customer master data with contact info. | `X-API-Key` |
| `POST /qbo/{id}/vendors` | Creates Vendor master data with contact info. | `X-API-Key` |
| `POST /qbo/{id}/items` | Creates Items (Service/NonInventory/Inventory). | `X-API-Key` |
| `PATCH /qbo/{id}/accounts/{account_id}` | Updates account name/number/description/active flag or parent. | `X-API-Key` |
| `GET /health` | Service health check. | Public |

### QBO AR/AP proxy

All `/qbo/{client_id}/{entity}` GET endpoints accept the same query parameters:

- `environment`: overrides the default client environment (`sandbox`/`prod`).
- `updated_since`: ISO timestamp applied to `MetaData.LastUpdatedTime`.
- `date_from` / `date_to`: filters by `TxnDate` (or `MetaData.CreateTime` for master data).
- `startposition` / `maxresults`: control pagination (default `1`/`100`, hard limit `1000`).
- `customer_ref`, `vendor_ref`, `doc_number`, `status`: optional filters mapped to the proper QBO fields per entity; unsupported combinations return HTTP 400.

Responses are normalized as:

```json
{
  "items": [{ "...": "QuickBooks entity JSON" }],
  "next_startposition": 101,
  "latency_ms": 212.5,
  "refreshed": false
}
```

Use `next_startposition` to continue pagination until it returns `null`.

#### Cash SalesReceipt (AR) payload

````json
POST /qbo/{client_id}/salesreceipts?auto_create=true
{
  "date": "2024-11-08",
  "doc_number": "AR-1007",
  "customer": "Acme Retail",
  "private_note": "Floor display refresh",
  "lines": [
    {
      "amount": 1500.00,
      "account_or_item": "Installation",
      "description": "Store setup",
      "class": "North America"
    },
    {
      "amount": 250.00,
      "account_or_item": "Travel Rebill",
      "description": "Mileage"
    }
  ]
}
````

#### Cash Expense (AP) payload

````json
POST /qbo/{client_id}/expenses?auto_create=true
{
  "date": "2024-11-09",
  "doc_number": "AP-2003",
  "vendor": "Logistica MX",
  "bank_account": "Checking",
  "private_note": "Freight inbound",
  "lines": [
    {
      "amount": 575.50,
      "expense_account": "Freight In",
      "description": "Containers",
      "class": "Imports"
    }
  ]
}
````

Rules:
- `Idempotency-Key` header is **required** for every POST route; fingerprints follow `realmId|entity|...` (per endpoint above) so retries return the exact same response body.
- `auto_create=true` allows the service to create a missing Customer or Vendor (DisplayName-based) on the fly; all other references (Items, Accounts, Classes, bank accounts) must already exist and can be referenced by QuickBooks Id or name.
- When posting Deposits or Expenses against the **sandbox** environment, missing vendors/customers and referenced bank/income/expense accounts are auto-created to streamline testing (the same behavior can be forced in any environment via `?auto_create=true`).
- References (`CustomerRef`, `VendorRef`, `AccountRef`, `ItemRef`, `ClassRef`) are resolved lazily and cached for the lifetime of the request to minimize duplicate lookups.

#### QBO AR/AP proxy (Invoices, Bills, Deposits, Payments, BillPayments, Items, Customers, Vendors, Accounts)

- **POST /qbo/{client_id}/invoices** — Creates an Invoice (AR) with `date`, optional `doc_number`, `customer`, optional header `class`/`private_note`, optional `txn_id`, and `lines[]` (`amount`, `account_or_item`, `description`, optional `class`). Fingerprint: `realmId|Invoice|date|total_amount|customer|doc_number|txn_id`.
- **POST /qbo/{client_id}/bills** — Creates an AP Bill with the same line schema (item or account). Fingerprint: `realmId|Bill|date|total_amount|vendor|doc_number|txn_id`.
- **POST /qbo/{client_id}/deposits** - Sends a Deposit to `deposit_to_account` with `lines[]` pointing to income accounts or Items (uses the item's income account). Each line can optionally include `entity_name` + `entity_type` (Customer/Vendor/Employee/Other) mirroring “Received From”. Fingerprint: `realmId|Deposit|date|total_amount|deposit_to_account|txn_id`.
- **POST /qbo/{client_id}/payments** — Applies a Payment to invoices; each line requires `linked_doc` (Invoice DocNumber or TxnId) plus the standard `amount`/`account_or_item` fields. Optional `deposit_to_account` and `ar_account` are supported. Fingerprint: `realmId|Payment|date|total_amount|customer|ref_doc_numbers|txn_id`.
- **POST /qbo/{client_id}/billpayments** - Applies a BillPayment to bills and requires a `bank_account`, `payment_type` (Check or CreditCard) and `lines[]` with `linked_doc`. Fingerprint: `realmId|BillPayment|date|total_amount|vendor|ref_doc_numbers|txn_id`.
- **POST /qbo/{client_id}/customers** / **POST /qbo/{client_id}/vendors** - Creates master data with `display_name`, optional `email`, `phone`, and `address.line1/line2/city/state/postal_code/country`. Fingerprint: `realmId|Customer|display_name|email/phone` (or `Vendor` for vendors).
- **POST /qbo/{client_id}/items** - Creates Service/NonInventory/Inventory items with `name`, `type`, `income_account`, optional `expense_account`, `asset_account`, `quantity_on_hand`, `inventory_start_date`, `sku`, `description`, and `active`. Fingerprint: `realmId|Item|name|type|sku`.
- **GET /qbo/{client_id}/accounts** - Lists the Chart of Accounts with pagination (`startposition`/`maxresults` up to 1000) and filters (`environment`, `updated_since`, `account_type`, `classification`, `active`). Response mirrors other collection proxies (`items[]`, `next_startposition`, `latency_ms`, `refreshed`).
- **GET /qbo/{client_id}/accounts/{account_id}** - Account detail lookup by Id, Name, or FullyQualifiedName. Returns the raw QuickBooks `Account` payload plus latency/refresh metadata.
- **PATCH /qbo/{client_id}/accounts/{account_id}** - Updates `name`, `account_number`, `description`, `active`, or `parent_account` while preserving `AccountType`/`Classification`. The service keeps the existing `SyncToken` and immutable fields intact for QuickBooks updates.

**Chart of Accounts quickstart**
- List with `GET /qbo/{client_id}/accounts?account_type=Bank&classification=Asset&active=true` (optional `environment`, `updated_since`, `startposition`, `maxresults`).
- Fetch detail with `GET /qbo/{client_id}/accounts/{account_id}` where `account_id` can be the QBO Id, `Name`, or `FullyQualifiedName` (e.g., `Bank:Checking`).
- Update with `PATCH /qbo/{client_id}/accounts/{account_id}` supplying any of `name`, `account_number`, `description`, `active`, or `parent_account` (Id or name). Account type/classification remain read-only by design.

#### Client summaries

`GET /clients?summary=1` enriches each entry with credential metadata without exposing tokens:

- `has_credentials`: whether any credential exists (respecting the optional `env` filter).
- `environments`: sorted list of environments with stored credentials (`["sandbox"]`, `["prod"]`, etc.).
- `access_status`: `valid`, `expired`, or `none`, based on the latest `access_expires_at`.
- `access_expires_at`: latest expiration considered for the status (or `null`).

Add `env=sandbox` or `env=production` to scope the calculation to a single environment. Omitting `summary` keeps the previous response shape exactly intact.

Error responses (JSON):
```json
{
  "code": 401,
  "message": "Invalid or missing API key",
  "details": null,
  "correlation_id": "8f2e4e24..."
}
```

## Postman Collection
The `postman/qbo-gateway.postman_collection.json` file includes:
- Variables: `base_url`, `api_key`, `client_id`, `environment`, OAuth helpers, and AR/AP shortcuts such as `customer_name`, `vendor_name`, `bank_account_name`, `deposit_account_name`, `ar_account_name`, `ap_account_name`, `income_account_name`, `expense_account_name`, and `asset_account_name`.
- Preconfigured requests for onboarding (`connect`, `callback`), CRUD clients, credentials, `companyinfo`, all collection GET routes (customers/vendors/items/invoices/payments/salesreceipts/expenses/bills/billpayments/deposits), and every POST/PATCH route (SalesReceipt, Invoice, Expense, Bill, Deposit, Payment, BillPayment, Customer, Vendor, Item, Account update) with sandbox-ready example bodies and headers (`X-API-Key`, `Idempotency-Key`).

Import into Postman, update the variables, and reuse the same `Idempotency-Key` to confirm the cached response behaviour for each write.

## Observability and Logging
- Structured JSON logs (`stdout`) include `request_id`, `client_id`, `realm_id`, latency, and retry metadata.
- Docker healthcheck calls `/health` every 30s.
- Retries for Intuit handle `429` and `5xx` responses, respecting `Retry-After`.
- Basic metrics (refreshes, 401, 429) emitted through logs.

## Testing Plan

### Unit Tests (pending)
1. **Fernet encryption** — verify `encrypt_refresh_token` + `decrypt_refresh_token` are inverses and fail with the wrong key.  
2. **Idempotency** — send the same `Idempotency-Key` twice: confirm cached response and conflict on mismatched payload.  
3. **Expiration logic** — force `access_expires_at` within 4 min and verify `ensure_valid_access_token` triggers refresh.  
4. **OAuth URL construction** — validate scopes and redirect correctness in `build_authorization_url`.

### Integration Tests (Intuit Sandbox)
1. `POST /clients` → create client "ACME" (with `Idempotency-Key`).  
2. `GET /auth/connect?client_id=<ACME>&env=sandbox` → complete Intuit authorization.  
3. Verify DB: table `client_credentials` contains `realm_id`, `refresh_token_enc`, consistent expirations.  
4. `GET /clients/<id>/credentials` → confirm expiration values and absence of tokens.  
5. `GET /qbo/<id>/companyinfo` → returns 200 with actual QuickBooks JSON.  
6. `POST /clients/<id>/credentials/rotate` → force refresh and repeat.  
7. Omit `X-API-Key` → expect 401 response.  
8. `POST /qbo/<id>/salesreceipts` → create a cash SalesReceipt and confirm it appears inside the QBO UI.  
9. `POST /qbo/<id>/expenses` → create a cash Expense against the sandbox BANK account and verify inside QBO.  
10. Repeat either POST with the same `Idempotency-Key` → expect the cached response body.  
11. Force a 401 (e.g., revoke the access token) and call `/qbo/<id>/customers` → verify auto refresh + retry.  
12. Call `/qbo/<id>/invoices?maxresults=1` twice → the second call should use `next_startposition` from the first response.

### Manual AR/AP validation
1. `POST /qbo/{id}/customers`, `/vendors`, and `/items` with the sandbox helpers to seed master data.
2. `POST /qbo/{id}/invoices?auto_create=true` -> confirm the Invoice appears in the QBO UI for the selected customer and class.
3. `POST /qbo/{id}/payments` applying the invoice DocNumber -> verify the invoice is marked paid inside QBO.
4. `POST /qbo/{id}/bills` followed by `POST /qbo/{id}/billpayments` -> confirm the Bill balance drops to zero.
5. `POST /qbo/{id}/deposits` targeting a BANK account and inspect the register in QBO.
6. `POST /qbo/{id}/expenses` against a BANK account (cash purchase) to ensure the legacy flow still works.
7. `GET /qbo/{id}/accounts?classification=Asset&account_type=Bank` -> verify filters/pagination and the `next_startposition` cursor.
8. `GET /qbo/{id}/accounts/Bank:Checking` -> confirm Name/FullyQualifiedName resolution in detail responses.
9. `PATCH /qbo/{id}/accounts/{account_id}` updating `account_number` or `parent_account` -> validate the changes inside the chart of accounts.
10. Repeat select POST calls with the same `Idempotency-Key` (Invoice, Payment, BillPayment, Deposit) to confirm cached responses.
### Acceptance Criteria
- Docker service responds to `GET /health` (200).  
- Complete sandbox OAuth flow with encrypted tokens in DB.  
- `GET /qbo/{client_id}/companyinfo` returns valid data and auto-refreshes if expired.  
- Collection endpoints `/qbo/{client_id}/{entity}` (customers, invoices, etc.) honor filters/pagination and include `next_startposition`.  
- Chart of Accounts endpoints (`GET /qbo/{client_id}/accounts`, detail, PATCH) honor filters and return proxy envelopes with latency/refresh metadata.  
- Cash `POST /qbo/{client_id}/salesreceipts` and `POST /qbo/{client_id}/expenses` enforce idempotency + optional auto-create of payees.  
- API key required; docs accessible without auth (configurable via `ALLOW_DOCS_WITHOUT_AUTH`).  
- Logs contain no secrets; include `client_id` and `realm_id` for traceability.  
- Postman collection works; README enables full environment reproduction.

## Suggested Next Steps
1. Persist QuickBooks entity IDs for every POST response and expose lookups so Finance can correlate retries without re-querying Intuit.
2. Add a lightweight cache (Redis or similar) around the reference resolver to reduce duplicate QBO `query` calls for popular Accounts/Items.
3. Build synthetic integration tests (GitHub Actions + sandbox credentials) that exercise the new Invoice/Bill/Payment/BillPayment flows nightly.
4. Stream structured audit events (e.g., to Kafka or Cloud Logging) each time an AR/AP write succeeds or fails, enabling downstream reconciliation.
5. Extend the API with optional attachments (PDF/XML) by using the QBO Upload API, so invoices and bills can include supporting documents.
## Reference Resolution & Auto-Create (Design Notes)

- `QBOReferenceResolver` centraliza los lookups con caché en memoria. Las búsquedas usan `select * from <Entity> where ... startposition=1 maxresults=1`. Errores de OAuth o API devuelven HTTP 502.
- `resolve_customer` / `resolve_vendor`: `UPPER(DisplayName) = '<identifier>'`; si `auto_create` se envía `POST /customer` o `POST /vendor` con `DisplayName`.
- `resolve_account`: `UPPER(Name) = '<identifier>'`; si se pasa `account_type`, agrega `AccountType = '<type>'`. No usa `FullyQualifiedName` ni reintenta sin tipo. Activos e inactivos se incluyen (no hay filtro `Active`).
- `ensure_account`: intenta `resolve_account` y si 404 + `auto_create`, llama a `_create_account` con `Name` saneado (reemplaza `:` por espacio, elimina tabs/newlines) y opcionalmente `AccountType` + `AccountSubType`.
- `resolve_item`: `FullyQualifiedName` case-sensitive (QuickBooks rechaza `UPPER()` para Item). `resolve_class` usa `FullyQualifiedName` case-sensitive. `resolve_entity_with_auto_create` solo auto-crea Customer/Vendor.
- `_execute_qbo_post` maneja idempotencia y logging; los handlers construyen payloads con refs resueltas antes de llegar aquí.

### Por qué aparece `6240 Duplicate Name Exists Error` al crear Accounts

- En Expenses con `auto_create=true` (o sandbox), cada línea hace: `resolve_account(line.expense_account)` (Name exacta); si 404, `ensure_account` con `AccountType='Expense'` y `AccountSubType='OfficeGeneralAdministrativeExpenses'`. El Name enviado al POST se sanea.
- Si se envía un FullyQualifiedName (ej. `Delivery COGS:Labor Cost`), el lookup usa `Name` en vez de `FullyQualifiedName`; la cuenta real puede existir como subcuenta (`Name='Labor Cost'`, `FullyQualifiedName='Delivery COGS:Labor Cost'`) o con otro `AccountType` (p.ej. `Cost of Goods Sold`). El filtro `AccountType='Expense'` impide verla.
- Tras no encontrar nada, `_create_account` intenta crear `Name='Delivery COGS Labor Cost'` (dos diferencias: saneo y `AccountType=Expense`). QuickBooks responde 6240 porque ya existe una cuenta con ese display name/FullyQualifiedName bajo otro tipo o jerarquía.
- Filtros que pueden ocultar la cuenta antes de auto-crear: `AccountType` distinto, uso de `Name` (no `FullyQualifiedName`), subcuentas donde el nombre del hijo no incluye al padre, y posibles cuentas inactivas con otro tipo que no coinciden con el filtro de tipo.

### Resumen por endpoint (POST)

| Endpoint | Resolución de referencias | Auto-create | Consultas típicas |
|----------|--------------------------|-------------|-------------------|
| `/qbo/{id}/expenses` | Vendor (`DisplayName`), Bank Account `AccountType=Bank`, líneas: Account (Name) o auto-create Expense | Vendor auto-create si `auto_create`/sandbox; Bank auto-create; líneas crean Expense account si 404 | Accounts: `select * from Account where AccountType='Expense' AND UPPER(Name)=...` al auto-crear |
| `/qbo/{id}/deposits` | DepositToAccount `AccountType=Bank`; líneas: Account (Name) o Item income account; optional Entity (Customer/Vendor/Employee/Other), Class | Auto-create Bank acct e Income acct si `auto_create`/sandbox; Entity auto-create solo Customer/Vendor | Accounts: `select * from Account where AccountType='Bank' AND UPPER(Name)=...`; Income auto-create: `AccountType='Income'` |
| `/qbo/{id}/invoices` | Customer, líneas: Item (FullyQualifiedName) o Account (Name), Class | Sin auto-create de cuentas; Customer auto-create si `auto_create` | Accounts: `select * from Account where UPPER(Name)=...` |
| `/qbo/{id}/salesreceipts` | Igual que invoices | Igual | Igual |
| `/qbo/{id}/bills` | Vendor, líneas: Item o Account (Name), Class | Sin auto-create | Igual |
| `/qbo/{id}/payments` | Customer; optional DepositToAccount/ARAccount (Name) | Sin auto-create | Igual |
| `/qbo/{id}/billpayments` | Vendor; bank/cc account (Name) con `AccountType=Bank|Credit Card`; optional APAccount | Sin auto-create | Accounts con filtro `AccountType` según `payment_type` |
| `/qbo/{id}/items` | IncomeAccount, optional Expense/Asset accounts (Name) | Sin auto-create | Igual |
| `/qbo/{id}/customers` | N/A (directo a POST) | N/A | N/A |
| `/qbo/{id}/vendors` | N/A (directo a POST) | N/A | N/A |

### Posibles inconsistencias

- Solo Expenses y Deposits auto-crean cuentas; el resto falla en 404 sin reintentos más laxos.
- Cuando se fuerza `AccountType` (Bank/Income/Expense) en `ensure_account`, no hay reintento sin tipo, de modo que una cuenta existente con otro tipo no se reutiliza y puede terminar en 6240.
- Los lookups de Account usan `Name`, no `FullyQualifiedName`; depósitos/bills/invoices no encuentran subcuentas si se envía el FQN.
- `auto_create` se extiende implícitamente a sandbox para Expenses/Deposits, pero no para otros endpoints.

### Candidate changes (no implementados aún)

- Si el identificador contiene `:` o falla con 6240, reintentar lookup por `FullyQualifiedName` y sin `AccountType`, reutilizando la cuenta existente.
- En `_create_account`, mapear `ParentRef` cuando se reciba un FQN (`A:B` → parent `A`, name `B`) para evitar nuevos nombres saneados.
- Normalizar la búsqueda de Accounts para que primero use `Name`, luego `FullyQualifiedName`, y solo después aplique `AccountType` como filtro secundario.
- Unificar la semántica de `auto_create` (decidir si sandbox siempre auto-crea o respetar el flag en todos los endpoints) y documentarlo en la API pública.

# Annex A — ETL to Deposit Mapping

## Deposit Transaction (ETL → QBO Deposit / POST /deposits)

### Header

| ETL Source Field              | Deposit POST Field     | Notes |
|------------------------------|------------------------|-------|
| bank_account + bank_cc_num   | deposit_to_account     | Bank account name/code in QBO (e.g., `DMD 2035`). |
| date                         | date                   | Transaction date. |
| txn_id                       | doc_number             | Unique transaction ID from the ETL (appears as “Ref no.” in QBO). |
| txn_id                       | txn_id                 | Same value; used for internal traceability in the gateway. |
| txn_id                       | **Idempotency-Key (header)** | Idempotency key for the POST request to the gateway. |
| drive_location (if present)  | private_note           | Optional memo (e.g., link to Drive folder). |

### Lines[n]

| ETL Source Field              | Deposit POST Field        | Notes |
|------------------------------|---------------------------|-------|
| description + extended_description | lines[n].description  | Full line description for the deposit. |
| amount (must be > 0)         | lines[n].amount           | Deposit amount. If the ETL amount is negative, convert to positive. |
| qbo_account `\|` qbo_sub_account | lines[n].account_or_item | Income account for the deposit line (e.g., `Delivery Revenue`). `qbo_account` is used; fallback to `qbo_sub_account` only if applicable. |
| payee_vendor                 | lines[n].entity_name      | Name shown as **Received From** in QBO (e.g., `STRONGHOLD`). |
| Business rules / classification | lines[n].entity_type   | `"Customer"` / `"Vendor"` / `"Other"` according to finance classification. |
| (not present in ETL)         | lines[n].class            | Optional; currently sent as empty. |
| (not present in ETL)         | lines[n].linked_doc       | Optional for future document linking; currently sent as empty. |

---

# Annex B — ETL to Expense Mapping

## Expense Transaction (ETL → QBO Purchase / POST /expenses)

### Header

| ETL Source Field              | Expense POST Field       | Notes |
|------------------------------|--------------------------|-------|
| payee_vendor                 | vendor                   | Vendor name shown on the Expense. |
| bank_account + bank_cc_num   | bank_account             | Bank/credit card account used to pay the expense (e.g., `DMD 71000`). |
| date                         | date                     | Expense date. |
| txn_id                       | doc_number               | Unique transaction ID from the ETL (appears as “Ref no.” in QBO). |
| txn_id                       | **Idempotency-Key (header)** | Idempotency key for the POST request to the gateway. |
| drive_location (if present)  | private_note             | Optional memo (e.g., link to Drive folder). |

### Lines[n]

| ETL Source Field                    | Expense POST Field        | Notes |
|------------------------------------|---------------------------|-------|
| amount (negative in ETL)           | lines[n].amount           | Expense amount. The ETL amount is negative; send `abs(amount)` as a positive number. |
| qbo_account `\|` qbo_sub_account   | lines[n].expense_account  | Expense account for the line (e.g., `Cost of Production:Production Supplies`). `qbo_account` is used; fallback to `qbo_sub_account` only if applicable. |
| description + extended_description | lines[n].description      | Full line description for the expense. |
| (not present in ETL)               | lines[n].class            | Optional; currently sent as empty. |

## Tracing & Observability

- Cada POST de QBO (deposits, expenses, invoices, etc.) emite logs estructurados `qbo_txn_attempt_started` y `qbo_txn_attempt_finished`.
- Campos clave para correlacionar desde Airflow/Kibana: `request_id`, `idempotency_key`, `doc_number`/`txn_id`, `client_id`, `realm_id`, `txn_type`, `environment`.
- En `qbo_txn_attempt_started` se incluye el payload sanitizado (sin tokens ni secretos).
- En `qbo_txn_attempt_finished` se registran: `gateway_status_code`, `qbo_status_code` (si aplica), `latency_ms`, `result=success|failure`, `error_code`, `error_message`, `qbo_error_details` (recortado).
- Las excepciones no controladas generan `qbo_txn_unhandled_exception` con `request_id`, `path` y `method`.
