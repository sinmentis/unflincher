from starlette.requests import Request

from diary.templates_env import LANG_COOKIE_NAME, get_current_language, templates


def _fake_request(cookie_header: str | None) -> Request:
    headers = [(b"cookie", cookie_header.encode())] if cookie_header else []
    scope = {"type": "http", "headers": headers}
    return Request(scope)


def test_lang_cookie_name():
    assert LANG_COOKIE_NAME == "diary_lang"


def test_get_current_language_defaults_to_english_with_no_cookie():
    assert get_current_language(_fake_request(None)) == "en"


def test_get_current_language_reads_supported_cookie():
    assert get_current_language(_fake_request("diary_lang=zh-Hans")) == "zh-Hans"


def test_get_current_language_falls_back_for_unsupported_cookie_value():
    assert get_current_language(_fake_request("diary_lang=klingon")) == "en"


def test_templates_env_is_a_single_shared_instance():
    # every route module must import the SAME object, not create its own --
    # otherwise each would need its own context_processors registration.
    from diary.routes import chat, entry, new_entry, report, timeline, workshop

    assert chat.templates is templates
    assert entry.templates is templates
    assert new_entry.templates is templates
    assert report.templates is templates
    assert timeline.templates is templates
    assert workshop.templates is templates
