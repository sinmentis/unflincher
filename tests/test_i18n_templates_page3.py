def test_workshop_page_translates(client):
    client.cookies.set("unflincher_lang", "de")
    res = client.get("/workshop")
    assert "Prompt-Einstellungen" in res.text
    assert "Persona-Prompt" in res.text
    assert "Anwenden" in res.text
