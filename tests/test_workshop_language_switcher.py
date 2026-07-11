def test_workshop_renders_language_select_with_all_supported_languages(client):
    from diary.i18n import SUPPORTED_LANGUAGE_CODES, t

    client.cookies.set("diary_lang", "en")
    res = client.get("/workshop")
    for code in SUPPORTED_LANGUAGE_CODES:
        assert f'value="{code}"' in res.text
        assert t("en", f"language.name.{code}") in res.text


def test_workshop_language_select_marks_current_language_selected(client):
    client.cookies.set("diary_lang", "ko")
    res = client.get("/workshop")
    assert 'value="ko" selected' in res.text
