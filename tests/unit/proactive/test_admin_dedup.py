"""Verify admin alert dedup uses atomic SET NX."""

import inspect

from adaptive_lang_study_bot.proactive.admin_reports import _send_alert_if_not_deduped


class TestAdminAlertDedup:

    def test_dedup_uses_set_nx_not_exists(self):
        """Alert dedup must use atomic SET NX, not check-then-set with exists()."""
        source = inspect.getsource(_send_alert_if_not_deduped)
        # Should NOT use exists() followed by set()
        assert "redis.exists(" not in source, (
            "Admin alert dedup should use redis.set(..., nx=True) instead of exists()+set()"
        )

    def test_dedup_uses_nx_flag(self):
        """Alert dedup SET call should include nx=True."""
        source = inspect.getsource(_send_alert_if_not_deduped)
        assert "nx=True" in source, (
            "Admin alert dedup should use redis.set(..., nx=True) for atomic dedup"
        )
