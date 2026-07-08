import diary.llm as llm_module


async def _fake_preview_tokens(*args, **kwargs):
    for t in ["预览：", "这版语气", "更温和"]:
        yield t


def _seed_entries(db):
    return [
        db.execute(
            "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
            "entry_date, source) VALUES (?, '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')",
            (f"日记{i}",),
        ).lastrowid
        for i in range(2)
    ]


def test_workshop_page_shows_active_prompt_and_entry_dropdown(client):
    db = client.app.state.db
    _seed_entries(db)

    response = client.get("/workshop")

    assert response.status_code == 200
    assert "人生导师" in response.text  # default persona prompt text
    assert "日记0" in response.text and "日记1" in response.text


def test_test_run_streams_but_writes_nothing_to_db(client, monkeypatch):
    monkeypatch.setattr(llm_module, "generate_commentary", _fake_preview_tokens)
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    before_prompts = db.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    before_commentary = db.execute("SELECT COUNT(*) AS n FROM entry_commentary").fetchone()["n"]

    response = client.post(
        "/workshop/test-run",
        json={"draft_prompt": "一个完全不同的草稿人设", "entry_id": entry_ids[0]},
    )

    assert response.status_code == 200
    # SSE frames separate every token, so the full concatenation never appears contiguously in
    # the raw body (rubber-duck correction #4). Assert the last token + terminal event instead.
    assert "更温和" in response.text
    assert "event: done" in response.text

    after_prompts = db.execute("SELECT COUNT(*) AS n FROM persona_prompt").fetchone()["n"]
    after_commentary = db.execute("SELECT COUNT(*) AS n FROM entry_commentary").fetchone()["n"]
    assert after_prompts == before_prompts
    assert after_commentary == before_commentary


def test_test_run_uses_full_corpus_same_as_real_generation(client, monkeypatch):
    captured = {}

    async def fake_generate(entry, all_entries, persona_text, model):
        captured["all_entries_count"] = len(all_entries)
        captured["persona_text"] = persona_text
        yield "x"

    monkeypatch.setattr(llm_module, "generate_commentary", fake_generate)
    db = client.app.state.db
    entry_ids = _seed_entries(db)

    client.post("/workshop/test-run", json={"draft_prompt": "草稿", "entry_id": entry_ids[0]})

    assert captured["all_entries_count"] == 2  # not just the 1 selected entry
    assert captured["persona_text"] == "草稿"


def test_apply_creates_new_active_prompt_version_without_generating(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("apply must not call the LLM")
    monkeypatch.setattr(llm_module, "generate_commentary", _boom)
    db = client.app.state.db

    response = client.post("/workshop/apply", json={"draft_prompt": "新的正式人设"})

    assert response.status_code == 200
    active = db.execute("SELECT body_text FROM persona_prompt WHERE is_active = 1").fetchone()
    assert active["body_text"] == "新的正式人设"
