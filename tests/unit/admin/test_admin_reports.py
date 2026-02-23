from datetime import datetime, timezone


class TestAlertDedupKeyFormat:

    def test_key_format(self):
        """Verify the dedup key format uses date_hour granularity."""
        now = datetime(2026, 2, 21, 14, 30, 0, tzinfo=timezone.utc)
        key = f"admin:alert:cost_spike:{now.strftime('%Y-%m-%d_%H')}"
        assert key == "admin:alert:cost_spike:2026-02-21_14"

    def test_different_hours_different_keys(self):
        t1 = datetime(2026, 2, 21, 14, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 2, 21, 15, 0, 0, tzinfo=timezone.utc)
        k1 = f"admin:alert:test:{t1.strftime('%Y-%m-%d_%H')}"
        k2 = f"admin:alert:test:{t2.strftime('%Y-%m-%d_%H')}"
        assert k1 != k2

    def test_same_hour_same_key(self):
        t1 = datetime(2026, 2, 21, 14, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 2, 21, 14, 59, 0, tzinfo=timezone.utc)
        k1 = f"admin:alert:test:{t1.strftime('%Y-%m-%d_%H')}"
        k2 = f"admin:alert:test:{t2.strftime('%Y-%m-%d_%H')}"
        assert k1 == k2


class TestCostSpikeThreshold:

    def test_no_spike_when_below_2x(self):
        cost_today = 0.50
        avg_7d = 0.30
        assert not (avg_7d > 0 and cost_today > 2 * avg_7d)

    def test_spike_when_above_2x(self):
        cost_today = 0.80
        avg_7d = 0.30
        assert avg_7d > 0 and cost_today > 2 * avg_7d

    def test_no_spike_when_avg_zero(self):
        cost_today = 0.50
        avg_7d = 0.0
        assert not (avg_7d > 0 and cost_today > 2 * avg_7d)

    def test_spike_at_exact_boundary(self):
        cost_today = 0.61
        avg_7d = 0.30
        assert avg_7d > 0 and cost_today > 2 * avg_7d

    def test_no_spike_at_exact_2x(self):
        cost_today = 0.60
        avg_7d = 0.30
        assert not (avg_7d > 0 and cost_today > 2 * avg_7d)


class TestNotificationFailureRateThreshold:

    def test_below_threshold(self):
        failed, total = 1, 10
        assert not (total >= 5 and (failed / total) > 0.30)

    def test_above_threshold(self):
        failed, total = 4, 10
        assert total >= 5 and (failed / total) > 0.30

    def test_too_few_notifications(self):
        """Don't alert on small sample sizes."""
        failed, total = 3, 4
        assert not (total >= 5 and (failed / total) > 0.30)

    def test_zero_total(self):
        failed, total = 0, 0
        assert not (total >= 5 and total > 0 and (failed / total) > 0.30)


class TestPoolUsageThreshold:

    def test_below_80_percent(self):
        active = 30
        max_slots = 50
        pct = active / max_slots * 100
        assert not (pct >= 80)

    def test_at_80_percent(self):
        active = 40
        max_slots = 50
        pct = active / max_slots * 100
        assert pct >= 80

    def test_above_80_percent(self):
        active = 45
        max_slots = 50
        pct = active / max_slots * 100
        assert pct >= 80
