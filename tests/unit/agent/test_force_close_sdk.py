"""Tests for _force_close_sdk_client and _kill_sdk_subprocess.

These helpers bypass the anyio TaskGroup __aexit__ path to avoid
RuntimeError when closing an SDK client from a different asyncio task
than the one that created it.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from adaptive_lang_study_bot.agent.session_manager import (
    _force_close_sdk_client,
    _kill_sdk_subprocess,
)


def _make_fake_client(*, has_query: bool = True, has_transport: bool = True) -> MagicMock:
    """Build a mock ClaudeSDKClient with controllable internals."""
    client = MagicMock()

    if not has_query:
        client._query = None
        client._transport = None
        return client

    # Transport mock
    transport = MagicMock()
    transport.close = AsyncMock()
    transport._process = MagicMock()
    type(transport._process).returncode = PropertyMock(return_value=None)

    # TaskGroup mock
    tg = MagicMock()
    tg.cancel_scope = MagicMock()

    # Query mock
    query = MagicMock()
    query._closed = False
    query._tg = tg
    query.transport = transport if has_transport else None

    client._query = query
    client._transport = transport if has_transport else None

    return client


# ---------------------------------------------------------------------------
# _force_close_sdk_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_close_marks_query_closed():
    client = _make_fake_client()
    await _force_close_sdk_client(client)
    assert client._query is None  # cleared after close


@pytest.mark.asyncio
async def test_force_close_cancels_task_group_scope():
    client = _make_fake_client()
    tg = client._query._tg
    await _force_close_sdk_client(client)
    tg.cancel_scope.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_force_close_detaches_task_group():
    """After force close, _tg should be set to None before transport.close()."""
    client = _make_fake_client()
    query = client._query

    # Track the order: _tg should be None before transport.close() is called
    tg_values_during_transport_close: list[object] = []

    original_close = client._query.transport.close

    async def track_close():
        tg_values_during_transport_close.append(query._tg)
        return await original_close()

    client._query.transport.close = track_close

    await _force_close_sdk_client(client)
    assert tg_values_during_transport_close == [None]


@pytest.mark.asyncio
async def test_force_close_calls_transport_close():
    client = _make_fake_client()
    transport = client._query.transport
    await _force_close_sdk_client(client)
    transport.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_close_clears_client_references():
    client = _make_fake_client()
    await _force_close_sdk_client(client)
    assert client._query is None
    assert client._transport is None


@pytest.mark.asyncio
async def test_force_close_noop_when_no_query():
    """Should not raise when client._query is already None."""
    client = _make_fake_client(has_query=False)
    await _force_close_sdk_client(client)  # should not raise


@pytest.mark.asyncio
async def test_force_close_handles_cancel_scope_error():
    """Should continue cleanup even if cancel_scope.cancel() raises."""
    client = _make_fake_client()
    client._query._tg.cancel_scope.cancel.side_effect = RuntimeError("boom")
    transport = client._query.transport
    await _force_close_sdk_client(client)
    # Transport should still be closed despite the error
    transport.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_close_handles_transport_close_error():
    """Should still clear references even if transport.close() raises."""
    client = _make_fake_client()
    client._query.transport.close = AsyncMock(side_effect=OSError("pipe broken"))
    await _force_close_sdk_client(client)
    assert client._query is None
    assert client._transport is None


@pytest.mark.asyncio
async def test_force_close_no_transport():
    """Should handle missing transport gracefully."""
    client = _make_fake_client(has_transport=False)
    await _force_close_sdk_client(client)
    assert client._query is None


@pytest.mark.asyncio
async def test_force_close_no_task_group():
    """Should handle None _tg gracefully."""
    client = _make_fake_client()
    client._query._tg = None
    transport = client._query.transport
    await _force_close_sdk_client(client)
    transport.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# _kill_sdk_subprocess
# ---------------------------------------------------------------------------


def test_kill_subprocess_sends_kill():
    client = _make_fake_client()
    _kill_sdk_subprocess(client)
    client._transport._process.kill.assert_called_once()


def test_kill_subprocess_noop_when_no_transport():
    client = _make_fake_client(has_query=False)
    _kill_sdk_subprocess(client)  # should not raise


def test_kill_subprocess_noop_when_already_exited():
    client = _make_fake_client()
    type(client._transport._process).returncode = PropertyMock(return_value=0)
    _kill_sdk_subprocess(client)
    client._transport._process.kill.assert_not_called()


def test_kill_subprocess_handles_exception():
    client = _make_fake_client()
    client._transport._process.kill.side_effect = ProcessLookupError("gone")
    _kill_sdk_subprocess(client)  # should not raise


def test_kill_subprocess_falls_back_to_query_transport():
    """When client._transport is None, should look at client._query.transport."""
    client = _make_fake_client()
    # Move transport to only be accessible via _query.transport
    transport = client._query.transport
    client._transport = None
    _kill_sdk_subprocess(client)
    transport._process.kill.assert_called_once()
