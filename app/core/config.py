from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "qbo-gateway"
    app_version: str = "0.1.0"
    environment: Literal["sandbox", "prod"] = Field(default="sandbox", alias="ENV")

    api_key: str = Field(..., alias="API_KEY")
    fernet_key: str = Field(..., alias="FERNET_KEY")

    qbo_client_id: str = Field(..., alias="QBO_CLIENT_ID")
    qbo_client_secret: str = Field(..., alias="QBO_CLIENT_SECRET")
    qbo_redirect_uri: HttpUrl = Field(..., alias="QBO_REDIRECT_URI")

    database_url: str = Field(..., alias="DATABASE_URL")

    http_timeout_seconds: float = Field(default=30.0, alias="HTTP_TIMEOUT_SECONDS")
    retry_max_attempts: int = Field(default=3, alias="RETRY_MAX_ATTEMPTS")
    retry_max_wait_seconds: float = Field(default=15.0, alias="RETRY_MAX_WAIT")

    allow_docs_without_auth: bool = Field(default=True, alias="ALLOW_DOCS_WITHOUT_AUTH")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
