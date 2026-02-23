"""Prometheus metrics definitions and HTTP server lifecycle.

All metrics are defined here as module-level objects. Instrumentation
points throughout the codebase import and call ``.inc()``, ``.observe()``,
or ``.set()`` on these objects.
"""

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from loguru import logger

# ---------------------------------------------------------------------------
# Gauges — current state
# ---------------------------------------------------------------------------

SESSION_POOL_ACTIVE = Gauge(
    "session_pool_active",
    "Currently active session pool slots",
    ["type"],  # interactive | proactive
)

SESSION_POOL_MAX = Gauge(
    "session_pool_max",
    "Maximum session pool capacity",
    ["type"],  # interactive | proactive
)

# ---------------------------------------------------------------------------
# Counters — cumulative totals
# ---------------------------------------------------------------------------

SESSIONS_CREATED = Counter(
    "sessions_created_total",
    "Total sessions created",
    ["tier", "session_type"],
)

SESSIONS_CLOSED = Counter(
    "sessions_closed_total",
    "Total sessions closed",
    ["tier", "reason"],
)

MESSAGES_PROCESSED = Counter(
    "messages_processed_total",
    "Total user messages processed by the agent",
    ["tier"],
)

SESSION_ERRORS = Counter(
    "session_errors_total",
    "Session lifecycle errors",
    ["stage"],  # create | process | close
)

NOTIFICATIONS_SENT = Counter(
    "notifications_sent_total",
    "Notifications dispatched to Telegram",
    ["type", "tier", "status"],
)

NOTIFICATIONS_SKIPPED = Counter(
    "notifications_skipped_total",
    "Notifications skipped before dispatch",
    ["reason"],  # skipped_paused | skipped_quiet | skipped_dedup | skipped_preference | skipped_limit
)

PROACTIVE_TICKS = Counter(
    "proactive_ticks_total",
    "Proactive tick executions",
)

PIPELINE_COMPLETED = Counter(
    "pipeline_completed_total",
    "Post-session pipeline completions",
    ["status"],  # completed | failed
)

# ---------------------------------------------------------------------------
# Histograms — distributions
# ---------------------------------------------------------------------------

SESSION_COST_USD = Histogram(
    "session_cost_usd",
    "Cost per session in USD",
    ["tier", "session_type"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0],
)

SESSION_DURATION_SECONDS = Histogram(
    "session_duration_seconds",
    "Session duration in seconds",
    ["tier"],
    buckets=[10, 30, 60, 120, 300, 600, 1200, 1800],
)

MESSAGE_COST_USD = Histogram(
    "message_cost_usd",
    "Cost per message in USD",
    ["tier"],
    buckets=[0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5],
)

PROACTIVE_TICK_DURATION = Histogram(
    "proactive_tick_duration_seconds",
    "Duration of a proactive tick execution",
    buckets=[0.1, 0.5, 1, 5, 10, 30, 60, 120],
)

PIPELINE_DURATION = Histogram(
    "pipeline_duration_seconds",
    "Duration of the post-session pipeline",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30],
)

NOTIFICATION_LLM_COST = Histogram(
    "notification_llm_cost_usd",
    "Cost of LLM-generated notifications",
    buckets=[0.001, 0.005, 0.01, 0.02, 0.05, 0.1],
)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def start_metrics_server(port: int) -> None:
    """Start the Prometheus metrics HTTP server on a daemon thread."""
    try:
        start_http_server(port)
        logger.info("Prometheus metrics server started on port {}", port)
    except OSError as e:
        logger.warning("Failed to start metrics server on port {}: {}", port, e)
