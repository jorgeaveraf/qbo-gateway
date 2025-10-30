# QBO Gateway Service

Microservicio HTTP basado en FastAPI que gestiona el ciclo de vida OAuth 2.0 de QuickBooks Online (Intuit) con rotación segura de tokens y expone endpoints para onboarding, administración de clientes y una primera operación proxy (`companyinfo`). El servicio está pensado para ser consumido por otros componentes (por ejemplo, Airflow) mediante HTTP y empaquetado en contenedores Docker.

## Tabla de contenidos
- [Arquitectura](#arquitectura)
- [Requisitos previos](#requisitos-previos)
- [Configuración de variables de entorno](#configuración-de-variables-de-entorno)
- [Ejecución en desarrollo (uv)](#ejecución-en-desarrollo-uv)
- [Ejecución con Docker Compose](#ejecución-con-docker-compose)
- [Migraciones de base de datos](#migraciones-de-base-de-datos)
- [Flujo OAuth con QuickBooks](#flujo-oauth-con-quickbooks)
- [API y seguridad](#api-y-seguridad)
- [Colección Postman](#colección-postman)
- [Observabilidad y logs](#observabilidad-y-logs)
- [Plan de pruebas](#plan-de-pruebas)
- [Próximos pasos sugeridos](#próximos-pasos-sugeridos)

## Arquitectura
- **FastAPI + Uvicorn** para el servidor HTTP.
- **SQLAlchemy 2.x** (async) y **Alembic** para persistencia (PostgreSQL recomendado; SQLite disponible para pruebas locales).
- **httpx + tenacity** para consumir endpoints de Intuit con reintentos y backoff.
- **cryptography (Fernet)** para cifrado de `refresh_token` en reposo.
- **JSON logging** con request-id, client-id y realmId para trazabilidad.
- **Idempotency keys** en peticiones `POST` para evitar duplicados.
- **Dockerfile multi-stage** y `docker-compose.yml` para un entorno reproducible.

```
qbo-gateway/
├─ app/
│  ├─ api/               # Routers FastAPI (auth, clients, qbo)
│  ├─ core/              # Configuración, logging, seguridad, HTTP client
│  ├─ db/                # Modelos, sesión async y repositorio
│  ├─ services/          # Integraciones con QBO (tokens y proxy)
│  ├─ schemas/           # Modelos Pydantic para I/O
│  └─ utils/             # Idempotencia, hashing y validaciones
├─ alembic/              # Migrations scripts
├─ Dockerfile
├─ docker-compose.yml
├─ postman/              # Colección Postman
├─ .env.example
└─ README.md
```

## Requisitos previos
- Python 3.11.x.
- [uv](https://github.com/astral-sh/uv) (gestor de dependencias rápido). Se instala con `pip install uv`.
- Docker y Docker Compose (para despliegues y paridad dev/prod).
- QuickBooks Online Sandbox app (Intuit Developer) con `client_id`, `client_secret` y redirect configurado.

> **Windows**: se recomienda usar WSL2 (Ubuntu) para igualar el entorno de producción y facilitar el uso de Docker Desktop.

## Configuración de variables de entorno
1. Copia el archivo `.env.example` a `.env`.
2. Ajusta los valores obligatorios:
   - `API_KEY`: clave que deben enviar los consumidores en `X-API-Key`.
   - `FERNET_KEY`: clave Base64 de 32 bytes. Puedes generar una con:
     ```bash
     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```
   - `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REDIRECT_URI`: obtenidos del portal Intuit Developer.
   - `DATABASE_URL`: idealmente `postgresql+psycopg://user:pass@host:5432/db`.
3. Ajusta timeouts y políticas de retry según tu escenario (`HTTP_TIMEOUT_SECONDS`, `RETRY_MAX_*`).

## Ejecución en desarrollo (uv)
```bash
# Crear entorno (opcional)
python -m venv .venv
source .venv/bin/activate  # .venv\Scripts\activate en Windows

# Instalar dependencias con uv
uv pip install --upgrade pip
uv pip install --system .

# Lanzar el servidor
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- OpenAPI disponible en `http://localhost:8000/docs` (Swagger) y `http://localhost:8000/openapi.json`.
- Health check: `GET http://localhost:8000/health`.

## Ejecución con Docker Compose
```bash
cp .env.example .env
docker compose up --build
```

Servicios:
- `api`: FastAPI con autoreload.
- `db`: PostgreSQL 16 con datos persistidos en el volumen `pgdata`.

Para detener: `docker compose down`. Añade `-v` si deseas eliminar el volumen local.

## Migraciones de base de datos

```bash
# Aplicar migraciones
uv run alembic upgrade head

# Crear una nueva migración (ejemplo)
uv run alembic revision -m "feature xyz"
uv run alembic upgrade head
```

La configuración de Alembic lee `DATABASE_URL` de tu `.env`. Para entornos Docker, ejecuta los comandos dentro del contenedor `api`.

## Flujo OAuth con QuickBooks
1. Registra el redirect en Intuit Developer: `https://<host>/auth/callback`.
2. Crea un cliente interno (`POST /clients`, enviando `Idempotency-Key`).
3. Inicia el onboarding:
   ```
   GET /auth/connect?client_id=<UUID>&env=sandbox
   ```
   Redirige a Intuit para conceder permisos (`com.intuit.quickbooks.accounting`).
4. Intuit llama al callback con `code`, `realmId` y `state`.
5. El servicio canjea el `code`, almacena de forma cifrada `refresh_token`, registra `access_token` y expiraciones.
6. Los endpoints `/clients/{id}/credentials` y `/qbo/{id}/companyinfo` usan y refrescan tokens automáticamente.

### Seguridad
- Todas las rutas salvo `/health`, `/docs`, `/openapi.json` requieren el header `X-API-Key`.
- `refresh_token` se guarda cifrado con Fernet; nunca se imprime en logs.
- Idempotencia soportada en `POST /clients` via header `Idempotency-Key`.

## API y seguridad

| Ruta | Descripción | Auth |
|------|-------------|------|
| `GET /auth/connect` | Redirección al login de Intuit para un cliente interno. | Requiere `X-API-Key`. |
| `GET /auth/callback` | Callback que canjea tokens y persiste credenciales. | Requiere `X-API-Key`. |
| `POST /clients` | Crea cliente (idempotente). | `X-API-Key` |
| `GET /clients` | Lista clientes. | `X-API-Key` |
| `GET /clients/{id}` | Detalle + credenciales. | `X-API-Key` |
| `PATCH /clients/{id}` | Actualiza datos. | `X-API-Key` |
| `DELETE /clients/{id}` | Elimina cliente y sus recursos asociados. | `X-API-Key` |
| `GET /clients/{id}/credentials` | Expone expiraciones (no tokens). | `X-API-Key` |
| `POST /clients/{id}/credentials/rotate` | Fuerza refresh y rotación. | `X-API-Key` |
| `GET /qbo/{id}/companyinfo` | Proxy a Intuit con refresh automático. | `X-API-Key` |
| `GET /health` | Estado del servicio. | Público |

Errores se responden en JSON:
```json
{
  "code": 401,
  "message": "Invalid or missing API key",
  "details": null,
  "correlation_id": "8f2e4e24..."
}
```

## Colección Postman
La colección `postman/qbo-gateway.postman_collection.json` incluye:
- Variables `base_url`, `api_key`, `client_id`, `environment`.
- Requests preconfigurados: onboarding (`connect`, `callback` con docs), CRUD de clientes, credenciales y `companyinfo`.

Importa la colección en Postman y actualiza las variables antes de ejecutar.

## Observabilidad y logs
- Logs estructurados en JSON (`stdout`), incluyen `request_id`, `client_id`, `realm_id`, latencias y reintentos.
- Healthcheck Docker consulta `/health` cada 30s.
- Retries para Intuit: reintenta `429` y `5xx`, respeta `Retry-After`.
- Métricas básicas (refresh, 401, 429) emitidas en logs.

## Plan de pruebas

### Pruebas unitarias (pendientes)
1. **Cifrado Fernet**: verificar que `encrypt_refresh_token` + `decrypt_refresh_token` son inversas y fallan con clave incorrecta.
2. **Idempotencia**: registrar la misma `Idempotency-Key` dos veces y asegurar respuesta cacheada + conflicto con payload distinto.
3. **Cálculo de expiración**: forzar `access_expires_at` a 4 min y comprobar que `ensure_valid_access_token` refresca.
4. **Construcción de URL OAuth**: validar scopes y redirect en `build_authorization_url`.

### Pruebas de integración (sandbox Intuit)
1. `POST /clients` → crear cliente “ACME” (usar `Idempotency-Key`).
2. `GET /auth/connect?client_id=<ACME>&env=sandbox` → completar autorización Intuit.
3. Verificar en BD: tabla `client_credentials` debe tener `realm_id`, `refresh_token_enc`, expiraciones coherentes.
4. `GET /clients/<id>/credentials` → confirmar expiraciones y ausencia de tokens en payload.
5. `GET /qbo/<id>/companyinfo` → respuesta 200 con JSON real de QuickBooks.
6. `POST /clients/<id>/credentials/rotate` → forzar refresh y repetir paso anterior.
7. Omitir `X-API-Key` y confirmar respuesta 401.

### Criterios de aceptación
- Servicio en Docker responde `GET /health` (200).
- Flujo OAuth sandbox completo con tokens cifrados en DB.
- `GET /qbo/{client_id}/companyinfo` devuelve datos reales y realiza refresh automático si expira.
- API Key obligatoria, documentación accesible sin header (configurable vía `ALLOW_DOCS_WITHOUT_AUTH`).
- Logs sin secretos, con trazabilidad por `client_id` y `realm_id`.
- Colección Postman funcional y README claro para reproducir entorno.

## Próximos pasos sugeridos
1. Añadir más endpoints QBO (invoices, payments) usando la misma estructura de proxy.
2. Implementar cache distribuido para access tokens si se necesita escalar horizontalmente.
3. Integrar herramientas de lint (`ruff`) y pruebas automáticas en CI.
4. Incorporar métricas Prometheus/Grafana o OpenTelemetry para observabilidad avanzada.
5. Planificar despliegue detrás de reverse proxy con TLS y rotación automatizada de FERNET key.
