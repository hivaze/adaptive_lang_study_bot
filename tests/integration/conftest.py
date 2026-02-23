"""Integration test fixtures — extends root conftest with DB-specific helpers.

Shared infrastructure (pg_container, pg_url, _create_tables, redis_container,
redis_client, mock_get_redis) is defined in tests/conftest.py.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)

from adaptive_lang_study_bot.db.models import User


# ---------------------------------------------------------------------------
# Transactional DB session (rolls back after each test)
# ---------------------------------------------------------------------------

@pytest.fixture()
async def db_session(pg_url: str) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional DB session that rolls back after each test.

    Creates a fresh engine per test bound to the test's own event loop,
    uses a transaction + SAVEPOINT for isolation, and rolls everything
    back at the end.
    """
    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        txn = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        nested = await conn.begin_nested()

        try:
            yield session
        finally:
            await session.close()
            if nested.is_active:
                await nested.rollback()
            if txn.is_active:
                await txn.rollback()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_COUNTER = 100_000


@pytest.fixture()
def make_user(db_session: AsyncSession) -> Callable[..., Coroutine[Any, Any, User]]:
    """Factory fixture to create a User with sensible defaults.

    Usage: ``user = await make_user(first_name="Alice")``
    """
    async def _factory(**overrides) -> User:
        global _USER_COUNTER
        _USER_COUNTER += 1
        defaults = {
            "telegram_id": _USER_COUNTER,
            "first_name": f"User{_USER_COUNTER}",
            "native_language": "en",
            "target_language": "es",
        }
        defaults.update(overrides)
        user = User(**defaults)
        db_session.add(user)
        await db_session.flush()
        return user

    return _factory
