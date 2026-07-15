import re

from unflincher.i18n import SUPPORTED_LANGUAGE_CODES

CJK_RANGE = re.compile(r"[\u4e00-\u9fff]")
# Strips the Perspective-instructions <textarea>'s inner content before the CJK scan. The
# instructions are user-facing AI configuration data (like diary content), not UI chrome. The
# i18n design explicitly never translates them, so the app's built-in default instructions
# (written in Chinese) legitimately renders untranslated on this page in every UI
# language. Without this strip, /workshop would always "fail" the chrome check for any
# language, for a reason that has nothing to do with a missed t() conversion.
PERSONA_TEXTAREA = re.compile(r'<textarea id="prompt-draft"[^>]*>.*?</textarea>', re.S)
# Strips the brand lockup's seal mark before the CJK scan. `诤` is an intentional,
# language-independent logo glyph (aria-hidden), part of the wordmark rather than translatable
# chrome, so it renders identically in every UI language and is not a missed t() conversion.
# Like the persona textarea above, it must be excluded or this check would always "fail" for a
# reason unrelated to localization.
BRAND_SEAL = re.compile(r'<span class="brand-seal"[^>]*>诤</span>')

PAGES = ["/", "/report", "/chat", "/new", "/workshop"]


def test_every_page_renders_without_error_in_every_language(client):
    for lang in SUPPORTED_LANGUAGE_CODES:
        client.cookies.set("unflincher_lang", lang)
        for page in PAGES:
            res = client.get(page)
            assert res.status_code == 200, f"{page} failed to render for lang={lang}"


def test_non_chinese_languages_have_no_leftover_hardcoded_chinese_chrome(client):
    # Chinese diary content itself (entry titles/bodies, which are real user data, not UI
    # chrome) is out of scope for this check -- this test only hits pages with no entries
    # seeded, so any CJK characters found here can only be untranslated leftover from a
    # non-Chinese, non-Japanese template string, not real diary content.
    #
    # "ja" is also excluded: Japanese legitimately writes most of its vocabulary in kanji
    # (Han characters), so the \u4e00-\u9fff range matches correct, natural Japanese UI
    # text just as much as it would match leftover untranslated Chinese. There is no
    # unicode-range-only way to distinguish "correct kanji" from "leftover hanzi" -- a
    # regression here has to be caught by eye (Task 6's Step 4 spot-check) or by a
    # dedicated per-key diff review, not by this blanket script-range assertion.
    for lang in [code for code in SUPPORTED_LANGUAGE_CODES if code not in ("zh-Hans", "ja")]:
        client.cookies.set("unflincher_lang", lang)
        for page in PAGES:
            res = client.get(page)
            text = PERSONA_TEXTAREA.sub("", res.text)
            text = BRAND_SEAL.sub("", text)
            leftover = CJK_RANGE.findall(text)
            assert not leftover, f"{page} still has hardcoded CJK chrome text for lang={lang}: {leftover}"
