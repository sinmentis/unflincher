# tests/test_i18n.py
import pytest

from unflincher.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGE_CODES, SUPPORTED_LANGUAGES, TRANSLATIONS, t


def test_supported_languages_exact_set():
    assert SUPPORTED_LANGUAGES == [
        ("en", "English"), ("zh-Hans", "简体中文"), ("ja", "日本語"), ("ko", "한국어"),
        ("es", "Español"), ("fr", "Français"), ("de", "Deutsch"), ("ru", "Русский"), ("pt", "Português"),
    ]


def test_supported_language_codes_derived_correctly():
    assert SUPPORTED_LANGUAGE_CODES == ["en", "zh-Hans", "ja", "ko", "es", "fr", "de", "ru", "pt"]


def test_default_language_is_english():
    assert DEFAULT_LANGUAGE == "en"


def test_all_languages_have_identical_key_sets():
    reference_keys = set(TRANSLATIONS["en"].keys())
    for lang in SUPPORTED_LANGUAGE_CODES:
        lang_keys = set(TRANSLATIONS[lang].keys())
        missing = reference_keys - lang_keys
        extra = lang_keys - reference_keys
        assert not missing, f"{lang} is missing keys: {sorted(missing)}"
        assert not extra, f"{lang} has extra keys not in en: {sorted(extra)}"


def test_t_returns_known_key_in_requested_language():
    assert t("zh-Hans", "nav.timeline") == "时间线"
    assert t("en", "nav.timeline") == "Timeline"


def test_t_falls_back_to_english_for_unknown_language():
    assert t("klingon", "nav.timeline") == "Timeline"


def test_t_falls_back_to_english_for_empty_string_language():
    assert t("", "nav.timeline") == "Timeline"


def test_t_interpolates_kwargs():
    result = t("en", "job.item_failed", entry_id=42, error="boom")
    assert result == "Entry #42 failed: boom"


def test_t_interpolates_kwargs_in_non_english_language():
    result = t("zh-Hans", "job.item_failed", entry_id=42, error="boom")
    assert result == "条目 #42 生成失败：boom"


def test_t_raises_keyerror_for_unknown_key():
    with pytest.raises(KeyError):
        t("en", "nonexistent.key.that.does.not.exist")


def test_startup_key_parity_assertion_actually_fires_on_a_broken_catalog():
    # Exercises the exact _assert_translation_key_parity() check that runs at import time --
    # simulates a translator accidentally deleting a key from one language's dict and confirms
    # the guard function (not just this test's own duplicate check) catches it.
    import unflincher.i18n as i18n_module

    broken = {lang: dict(cat) for lang, cat in i18n_module.TRANSLATIONS.items()}
    del broken["ja"]["nav.timeline"]

    original = i18n_module.TRANSLATIONS
    i18n_module.TRANSLATIONS = broken
    try:
        with pytest.raises(RuntimeError, match="out of sync"):
            i18n_module._assert_translation_key_parity()
    finally:
        i18n_module.TRANSLATIONS = original
