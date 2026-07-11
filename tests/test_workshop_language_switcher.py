def test_workshop_renders_language_select_with_all_supported_languages(client):
    from diary.i18n import SUPPORTED_LANGUAGES

    res = client.get("/workshop")
    for code, display_name in SUPPORTED_LANGUAGES:
        assert f'value="{code}"' in res.text
        assert display_name in res.text


def test_workshop_language_select_marks_current_language_selected(client):
    client.cookies.set("diary_lang", "ko")
    res = client.get("/workshop")
    assert 'value="ko" selected' in res.text
