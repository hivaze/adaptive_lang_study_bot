"""Tests for the i18n module."""

from adaptive_lang_study_bot.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_NATIVE_LANGUAGES,
    _load_locale,
    get_localized_language_name,
    reload_locales,
    t,
)


class TestTranslationFunction:

    def test_basic_key(self):
        result = t("chat.error", "en")
        assert "wrong" in result.lower() or "error" in result.lower()

    def test_variable_substitution(self):
        result = t("stats.streak", "en", days=42)
        assert "42" in result

    def test_missing_key_returns_key(self):
        result = t("nonexistent.key.here", "en")
        assert result == "nonexistent.key.here"

    def test_unsupported_lang_falls_back_to_english(self):
        result = t("chat.error", "zh")
        en_result = t("chat.error", "en")
        assert result == en_result

    def test_missing_key_in_locale_falls_back_to_english(self):
        """If a key is missing in the requested locale, fallback to English."""
        # Use a key that definitely exists in English
        en_result = t("chat.error", "en")
        # This should either have a translation or fallback to en
        result = t("chat.error", "ru")
        assert isinstance(result, str)
        assert len(result) > 0
        # It shouldn't be the raw key
        assert result != "chat.error"

    def test_list_valued_key_returns_string(self):
        """List values (notification variants) should return a single string."""
        result = t("notif.streak_risk", "en", name="Alex", streak=5, due_count=3)
        assert isinstance(result, str)
        assert len(result) > 10

    def test_default_language_is_en(self):
        assert DEFAULT_LANGUAGE == "en"

    def test_supported_languages_count(self):
        assert len(SUPPORTED_NATIVE_LANGUAGES) == 7
        assert SUPPORTED_NATIVE_LANGUAGES == frozenset({"en", "ru", "es", "fr", "de", "pt", "it"})


class TestGetLocalizedLanguageName:

    def test_english_in_english(self):
        assert get_localized_language_name("en", "en") == "English"

    def test_unknown_code_returns_key(self):
        result = get_localized_language_name("xx", "en")
        assert result == "lang.xx"


class TestLocaleFiles:

    def test_en_locale_loads(self):
        locale = _load_locale("en")
        assert isinstance(locale, dict)
        assert len(locale) > 100  # ~210 keys expected

    def test_all_locales_load(self):
        """All 7 locale files should load without errors."""
        reload_locales()
        for lang in SUPPORTED_NATIVE_LANGUAGES:
            locale = _load_locale(lang)
            assert isinstance(locale, dict), f"Failed to load {lang}.json"
            assert len(locale) > 0, f"{lang}.json is empty"

    def test_locale_completeness(self):
        """Every key in en.json should exist in all other locale files."""
        reload_locales()
        en_locale = _load_locale("en")
        en_keys = set(en_locale.keys())

        for lang in SUPPORTED_NATIVE_LANGUAGES:
            if lang == "en":
                continue
            locale = _load_locale(lang)
            locale_keys = set(locale.keys())
            missing = en_keys - locale_keys
            assert not missing, (
                f"{lang}.json is missing {len(missing)} keys: "
                f"{sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}"
            )

    def test_list_valued_keys_match(self):
        """List-valued keys should have the same number of variants across locales."""
        reload_locales()
        en_locale = _load_locale("en")
        list_keys = {k for k, v in en_locale.items() if isinstance(v, list)}

        for lang in SUPPORTED_NATIVE_LANGUAGES:
            if lang == "en":
                continue
            locale = _load_locale(lang)
            for key in list_keys:
                en_val = en_locale[key]
                loc_val = locale.get(key)
                if loc_val is None:
                    continue  # Caught by completeness test
                assert isinstance(loc_val, list), (
                    f"{lang}.json key '{key}' should be a list, got {type(loc_val).__name__}"
                )
                assert len(loc_val) == len(en_val), (
                    f"{lang}.json key '{key}' has {len(loc_val)} variants, "
                    f"expected {len(en_val)}"
                )


class TestReloadLocales:

    def test_reload_clears_cache(self):
        """reload_locales() should clear the lru_cache."""
        _ = _load_locale("en")
        reload_locales()
        # Should not raise
        _ = _load_locale("en")
