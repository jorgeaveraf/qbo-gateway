from __future__ import annotations

from functools import lru_cache
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+psycopg_async://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


@lru_cache(maxsize=1)
def get_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    normalized_url = normalize_database_url(settings.database_url)
    return create_async_engine(
        normalized_url,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    engine = get_engine(settings)
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session
