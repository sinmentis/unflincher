from unflincher.i18n import SUPPORTED_LANGUAGE_CODES, TRANSLATIONS

ONBOARDING_KEYS = (
    "timeline.empty_state",
    "timeline.empty_help",
    "timeline.import_archive_action",
    "timeline.write_first_entry_action",
    "onboarding.heading",
    "onboarding.step_choose_heading",
    "onboarding.step_choose_body",
    "onboarding.step_choose_action",
    "onboarding.step_preview_heading",
    "onboarding.step_preview_body",
    "onboarding.step_preview_action",
    "onboarding.step_report_heading",
    "onboarding.step_report_body",
    "onboarding.step_report_action",
    "report.generate_first_button",
    "report.no_report_help",
)


def test_onboarding_keys_exist_and_render_in_every_language():
    for lang in SUPPORTED_LANGUAGE_CODES:
        catalog = TRANSLATIONS[lang]
        for key in ONBOARDING_KEYS:
            assert key in catalog, f"{lang} is missing {key}"
            assert catalog[key].strip(), f"{lang}.{key} is empty"


def test_preview_copy_describes_the_unsaved_draft_not_the_active_prompt():
    copy = TRANSLATIONS["en"]["onboarding.step_preview_body"]
    assert "active Perspective" not in copy
    assert "draft" in copy
    assert "without saving the result" in copy


def test_no_entries_state_renders_in_every_language(client):
    for lang in SUPPORTED_LANGUAGE_CODES:
        client.cookies.set("unflincher_lang", lang)
        response = client.get("/")
        assert response.status_code == 200, f"/ failed to render for lang={lang}"
        body = response.text
        assert 'data-role="onboarding-panel"' not in body
        assert TRANSLATIONS[lang]["timeline.import_archive_action"] in body
        assert TRANSLATIONS[lang]["timeline.write_first_entry_action"] in body


def test_ready_to_reflect_state_renders_in_every_language(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'manual')"
    )
    for lang in SUPPORTED_LANGUAGE_CODES:
        client.cookies.set("unflincher_lang", lang)
        response = client.get("/")
        assert response.status_code == 200, f"/ failed to render for lang={lang}"
        body = response.text
        assert 'data-role="onboarding-panel"' in body
        assert TRANSLATIONS[lang]["onboarding.heading"] in body
        assert TRANSLATIONS[lang]["onboarding.step_report_action"] in body


def test_active_state_renders_in_every_language(client):
    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('t', '<p>a</p>', '<p>a</p>', 'a', '2026-01-01', 'manual')"
    ).lastrowid
    prompt_id = db.execute(
        "INSERT INTO persona_prompt (version_no, body_text, model, is_active) VALUES (2, 'p', 'm', 0)"
    ).lastrowid
    db.execute(
        "INSERT INTO entry_commentary (entry_id, prompt_version_id, model, body_text, status) "
        "VALUES (?, ?, 'm', 'take', 'ok')",
        (entry_id, prompt_id),
    )
    for lang in SUPPORTED_LANGUAGE_CODES:
        client.cookies.set("unflincher_lang", lang)
        response = client.get("/")
        assert response.status_code == 200, f"/ failed to render for lang={lang}"
        assert 'data-role="onboarding-panel"' not in response.text


def test_report_first_action_renders_in_every_language(client):
    for lang in SUPPORTED_LANGUAGE_CODES:
        client.cookies.set("unflincher_lang", lang)
        response = client.get("/report")
        assert response.status_code == 200, f"/report failed to render for lang={lang}"
        assert TRANSLATIONS[lang]["report.generate_first_button"] in response.text
