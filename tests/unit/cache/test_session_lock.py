"""Verify session_lock behavior and absence of dead code."""

import inspect

from adaptive_lang_study_bot.cache.session_lock import (
    acquire_session_lock,
    refresh_session_lock,
    release_session_lock,
)


class TestSessionLockNoDeadCode:

    def test_no_redis_unavailable_token_in_module(self):
        """REDIS_UNAVAILABLE_TOKEN should not exist in the module (dead code removed)."""
        import adaptive_lang_study_bot.cache.session_lock as mod
        assert not hasattr(mod, "REDIS_UNAVAILABLE_TOKEN"), (
            "REDIS_UNAVAILABLE_TOKEN should be removed — it was dead code"
        )

    def test_no_redis_unavailable_token_in_acquire(self):
        """acquire_session_lock must not reference REDIS_UNAVAILABLE_TOKEN."""
        source = inspect.getsource(acquire_session_lock)
        assert "REDIS_UNAVAILABLE_TOKEN" not in source

    def test_no_redis_unavailable_token_in_refresh(self):
        """refresh_session_lock must not reference REDIS_UNAVAILABLE_TOKEN."""
        source = inspect.getsource(refresh_session_lock)
        assert "REDIS_UNAVAILABLE_TOKEN" not in source

    def test_no_redis_unavailable_token_in_release(self):
        """release_session_lock must not reference REDIS_UNAVAILABLE_TOKEN."""
        source = inspect.getsource(release_session_lock)
        assert "REDIS_UNAVAILABLE_TOKEN" not in source

    def test_docstring_says_fails_closed(self):
        """Docstring should accurately describe fail-closed behavior."""
        doc = acquire_session_lock.__doc__ or ""
        assert "fails open" not in doc.lower(), (
            "Docstring incorrectly says 'fails open' — implementation returns None (fails closed)"
        )
