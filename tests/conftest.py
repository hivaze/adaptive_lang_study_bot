"""Root conftest — shared fixtures for integration and LLM test tiers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from adaptive_lang_study_bot.db.models import Base


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require Docker (PostgreSQL/Redis containers)",
    )
    config.addinivalue_line(
        "markers",
        "llm: marks tests that make real API calls to Claude (require ANTHROPIC_API_KEY)",
    )


# ---------------------------------------------------------------------------
# PostgreSQL (shared by integration + LLM tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def pg_container():
    """Start a throwaway PostgreSQL 16 container for the whole test session."""
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_url(pg_container) -> str:
    """Return the async connection URL for the test PostgreSQL container."""
    return pg_container.get_connection_url()


@pytest.fixture(scope="session", autouse=True)
def _create_tables(pg_url: str):
    """Create all tables once per session using a throwaway engine + loop."""
    async def _setup():
        engine = create_async_engine(pg_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())


# ---------------------------------------------------------------------------
# Redis (shared by integration + LLM tests)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def redis_container():
    """Start a throwaway Redis 7 container for the whole test session."""
    with RedisContainer("redis:7-alpine") as r:
        yield r


@pytest.fixture()
async def redis_client(redis_container) -> AsyncGenerator[Redis, None]:
    """Provide a Redis client that flushes DB after each test."""
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    url = f"redis://{host}:{port}/0"
    client = Redis.from_url(url, decode_responses=True)
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture()
def mock_get_redis(monkeypatch, redis_client: Redis):
    """Monkeypatch get_redis() to return the test Redis client."""
    async def _fake_get_redis() -> Redis:
        return redis_client

    monkeypatch.setattr(
        "adaptive_lang_study_bot.cache.client.get_redis", _fake_get_redis,
    )
    monkeypatch.setattr(
        "adaptive_lang_study_bot.cache.session_lock.get_redis", _fake_get_redis,
    )
