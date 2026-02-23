"""Centralized Redis key patterns and TTL constants.

All Redis keys used by the application are defined here to ensure
consistency and make it easy to audit the full Redis key space.
"""

# --- Key patterns ---

SESSION_LOCK_KEY = "session:active:{user_id}"
RATE_LIMIT_KEY = "ratelimit:user:{user_id}:minute"
NOTIF_DEDUP_KEY = "notif:dedup:{user_id}:{type}:{date}"
NOTIF_LLM_KEY = "notif:llm_count:{user_id}:{date}"
PROACTIVE_TICK_LOCK_KEY = "lock:proactive_tick"
ADMIN_HEALTH_LOCK_KEY = "lock:admin_health"
ADMIN_STATS_LOCK_KEY = "lock:admin_stats_report"
ADMIN_ALERT_DEDUP_KEY = "admin:alert:{alert_type}:{date_hour}"

# --- TTL constants (seconds) ---

RATE_LIMIT_WINDOW = 60
NOTIF_DEDUP_TTL = 86400  # 24 hours
PROACTIVE_TICK_LOCK_TTL = 300  # 5 minutes
ADMIN_HEALTH_LOCK_TTL = 120  # 2 minutes
ADMIN_STATS_LOCK_TTL = 300  # 5 minutes
ADMIN_ALERT_DEDUP_TTL = 3600  # 1 hour
