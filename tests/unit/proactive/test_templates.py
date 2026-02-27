from adaptive_lang_study_bot.i18n import _load_locale, t


def render_template(template_type: str, *, lang: str = "en", **kwargs: object) -> str:
    """Local helper — mirrors the deleted templates.py for test continuity."""
    return t(f"notif.{template_type}", lang, **kwargs)


_EXPECTED_TEMPLATE_TYPES = [
    "streak_risk", "cards_due", "user_inactive",
    "weak_area_persistent", "score_trend_improving",
    "score_trend_declining", "incomplete_exercise",
    "milestone_vocab", "milestone_streak",
    "weekly_summary_template",
    "weak_area_drill", "difficulty_changed",
    # Re-engagement
    "post_onboarding_24h", "post_onboarding_3d", "post_onboarding_7d",
    "lapsed_gentle", "lapsed_compelling", "lapsed_miss_you",
]


def test_all_template_types_exist_in_en():
    locale = _load_locale("en")
    for tpl_type in _EXPECTED_TEMPLATE_TYPES:
        key = f"notif.{tpl_type}"
        assert key in locale, f"Missing template key: {key}"


def test_all_templates_have_variants():
    locale = _load_locale("en")
    for tpl_type in _EXPECTED_TEMPLATE_TYPES:
        key = f"notif.{tpl_type}"
        variants = locale.get(key, [])
        assert isinstance(variants, list), f"Template '{key}' should be a list"
        assert len(variants) >= 1, f"Template '{key}' has no variants"


def test_render_streak_risk():
    result = render_template(
        "streak_risk", name="Alex", streak=12, due_count=5,
    )
    assert isinstance(result, str)
    assert len(result) > 10
    assert "12" in result or "5" in result or "Alex" in result


def test_render_cards_due():
    result = render_template(
        "cards_due", name="Alex", due_count=8,
    )
    assert "8" in result


def test_render_unknown_type():
    result = render_template("nonexistent_type")
    assert "nonexistent_type" in result


def test_render_milestone_vocab():
    result = render_template("milestone_vocab", name="Alex", count=300)
    assert "300" in result


def test_render_milestone_streak():
    result = render_template("milestone_streak", name="Alex", streak=20)
    assert "20" in result


def test_render_weak_area_drill():
    result = render_template("weak_area_drill", name="Alex", topic="subjunctive")
    assert "subjunctive" in result


def test_render_difficulty_changed():
    result = render_template("difficulty_changed", new_difficulty="hard")
    assert "hard" in result


def test_render_post_onboarding_24h():
    result = render_template("post_onboarding_24h", name="Alex", target_language="French")
    assert "Alex" in result
    assert "French" in result


def test_render_post_onboarding_3d():
    result = render_template("post_onboarding_3d", name="Alex", target_language="French")
    assert "Alex" in result


def test_render_post_onboarding_7d():
    result = render_template("post_onboarding_7d", name="Alex", target_language="French")
    assert "Alex" in result


def test_render_lapsed_gentle():
    result = render_template("lapsed_gentle", name="Alex", target_language="French")
    assert "Alex" in result


def test_render_lapsed_compelling():
    result = render_template(
        "lapsed_compelling", name="Alex", target_language="French",
        vocabulary_count=120, level="B1", sessions_completed=15,
    )
    assert "Alex" in result
    assert "120" in result or "B1" in result or "15" in result or "French" in result


def test_render_lapsed_miss_you():
    result = render_template(
        "lapsed_miss_you", name="Alex", target_language="French",
        vocabulary_count=200,
    )
    assert "Alex" in result


