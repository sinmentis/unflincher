import re

from diary.i18n import SUPPORTED_LANGUAGE_CODES

CJK_RANGE = re.compile(r"[\u4e00-\u9fff]")

PAGES = ["/", "/report", "/chat", "/new", "/workshop"]


def test_every_page_renders_without_error_in_every_language(client):
    for lang in SUPPORTED_LANGUAGE_CODES:
        client.cookies.set("diary_lang", lang)
        for page in PAGES:
            res = client.get(page)
            assert res.status_code == 200, f"{page} failed to render for lang={lang}"


def test_non_chinese_languages_have_no_leftover_hardcoded_chinese_chrome(client):
    # Chinese diary content itself (entry titles/bodies, which are real user data, not UI
    # chrome) is out of scope for this check -- this test only hits pages with no entries
    # seeded, so any CJK characters found here can only be undset from an untranslated
    # template string, not from real diary content.
    for lang in [l for l in SUPPORTED_LANGUAGE_CODES if l != "zh-Hans"]:
        client.cookies.set("diary_lang", lang)
        for page in PAGES:
            res = client.get(page)
            leftover = CJK_RANGE.findall(res.text)
            assert not leftover, f"{page} still has hardcoded CJK chrome text for lang={lang}: {leftover}"
