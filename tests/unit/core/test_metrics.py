"""Tests for the metrics module."""

from prometheus_client import Counter, Gauge, Histogram

from adaptive_lang_study_bot.metrics import (
    MESSAGE_COST_USD,
    MESSAGES_PROCESSED,
    NOTIFICATION_LLM_COST,
    NOTIFICATIONS_SENT,
    NOTIFICATIONS_SKIPPED,
    PIPELINE_COMPLETED,
    PIPELINE_DURATION,
    PROACTIVE_TICK_DURATION,
    PROACTIVE_TICKS,
    SESSION_COST_USD,
    SESSION_DURATION_SECONDS,
    SESSION_ERRORS,
    SESSION_POOL_ACTIVE,
    SESSION_POOL_MAX,
    SESSIONS_CLOSED,
    SESSIONS_CREATED,
    start_metrics_server,
)


class TestMetricTypes:

    def test_gauges(self):
        assert isinstance(SESSION_POOL_ACTIVE, Gauge)
        assert isinstance(SESSION_POOL_MAX, Gauge)

    def test_counters(self):
        assert isinstance(SESSIONS_CREATED, Counter)
        assert isinstance(SESSIONS_CLOSED, Counter)
        assert isinstance(MESSAGES_PROCESSED, Counter)
        assert isinstance(SESSION_ERRORS, Counter)
        assert isinstance(NOTIFICATIONS_SENT, Counter)
        assert isinstance(NOTIFICATIONS_SKIPPED, Counter)
        assert isinstance(PROACTIVE_TICKS, Counter)
        assert isinstance(PIPELINE_COMPLETED, Counter)

    def test_histograms(self):
        assert isinstance(SESSION_COST_USD, Histogram)
        assert isinstance(SESSION_DURATION_SECONDS, Histogram)
        assert isinstance(MESSAGE_COST_USD, Histogram)
        assert isinstance(PROACTIVE_TICK_DURATION, Histogram)
        assert isinstance(PIPELINE_DURATION, Histogram)
        assert isinstance(NOTIFICATION_LLM_COST, Histogram)


class TestMetricLabels:
    """Verify label names are consistent with usage in instrumented code."""

    def test_pool_gauges_have_type_label(self):
        # Should not raise
        SESSION_POOL_ACTIVE.labels(type="interactive")
        SESSION_POOL_ACTIVE.labels(type="proactive")
        SESSION_POOL_MAX.labels(type="interactive")
        SESSION_POOL_MAX.labels(type="proactive")

    def test_sessions_created_labels(self):
        SESSIONS_CREATED.labels(tier="free", session_type="interactive")

    def test_sessions_closed_labels(self):
        SESSIONS_CLOSED.labels(tier="free", reason="idle_timeout")

    def test_messages_processed_labels(self):
        MESSAGES_PROCESSED.labels(tier="free")

    def test_session_errors_labels(self):
        SESSION_ERRORS.labels(stage="create")
        SESSION_ERRORS.labels(stage="process")

    def test_notifications_sent_labels(self):
        NOTIFICATIONS_SENT.labels(type="streak_risk", tier="template", status="sent")

    def test_notifications_skipped_labels(self):
        NOTIFICATIONS_SKIPPED.labels(reason="skipped_paused")

    def test_pipeline_completed_labels(self):
        PIPELINE_COMPLETED.labels(status="completed")
        PIPELINE_COMPLETED.labels(status="failed")

    def test_session_cost_labels(self):
        SESSION_COST_USD.labels(tier="free", session_type="interactive")

    def test_session_duration_labels(self):
        SESSION_DURATION_SECONDS.labels(tier="free")

    def test_message_cost_labels(self):
        MESSAGE_COST_USD.labels(tier="free")


class TestStartMetricsServer:

    def test_callable(self):
        assert callable(start_metrics_server)
