from adaptive_lang_study_bot.config import TIER_LIMITS, TierLimits, UserTier


def test_tier_limits_exist():
    assert UserTier.FREE in TIER_LIMITS
    assert UserTier.PREMIUM in TIER_LIMITS


def test_free_tier_uses_haiku():
    limits = TIER_LIMITS[UserTier.FREE]
    assert isinstance(limits, TierLimits)
    assert "haiku" in limits.model


def test_premium_tier_uses_sonnet():
    limits = TIER_LIMITS[UserTier.PREMIUM]
    assert "sonnet" in limits.model


def test_free_tier_stricter_than_premium():
    free = TIER_LIMITS[UserTier.FREE]
    premium = TIER_LIMITS[UserTier.PREMIUM]

    assert free.max_turns_per_session < premium.max_turns_per_session
    assert free.max_cost_per_day_usd < premium.max_cost_per_day_usd
    assert free.session_idle_timeout_seconds < premium.session_idle_timeout_seconds
    assert free.rate_limit_per_minute < premium.rate_limit_per_minute
    assert free.max_llm_notifications_per_day < premium.max_llm_notifications_per_day


def test_both_tiers_thinking_adaptive():
    assert TIER_LIMITS[UserTier.FREE].thinking_type == "adaptive"
    assert TIER_LIMITS[UserTier.PREMIUM].thinking_type == "adaptive"


def test_premium_tier_unlimited_sessions():
    assert TIER_LIMITS[UserTier.PREMIUM].max_sessions_per_day == 0
