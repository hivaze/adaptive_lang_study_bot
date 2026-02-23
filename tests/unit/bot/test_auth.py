from adaptive_lang_study_bot.bot.middlewares.auth import (
    _SUPPORTED_LANGUAGES,
    _detect_native_language,
)


class TestDetectNativeLanguage:

    def test_none_returns_english(self):
        assert _detect_native_language(None) == "en"

    def test_empty_string_returns_english(self):
        assert _detect_native_language("") == "en"

    def test_simple_en(self):
        assert _detect_native_language("en") == "en"

    def test_simple_ru(self):
        assert _detect_native_language("ru") == "ru"

    def test_ietf_tag_pt_br(self):
        assert _detect_native_language("pt-BR") == "pt"

    def test_ietf_tag_zh_cn_falls_back(self):
        # zh is no longer a supported native language
        assert _detect_native_language("zh-CN") == "en"

    def test_ietf_tag_en_us(self):
        assert _detect_native_language("en-US") == "en"

    def test_unknown_language_returns_english(self):
        assert _detect_native_language("xx") == "en"

    def test_case_insensitive(self):
        assert _detect_native_language("RU") == "ru"
        assert _detect_native_language("Fr") == "fr"

    def test_all_supported_languages_detected(self):
        for lang in _SUPPORTED_LANGUAGES:
            assert _detect_native_language(lang) == lang

    def test_supported_languages_count(self):
        assert len(_SUPPORTED_LANGUAGES) == 7
