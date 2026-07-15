def test_workshop_page_translates(client):
    client.cookies.set("unflincher_lang", "de")
    res = client.get("/workshop")
    assert "Prompt-Werkstatt" in res.text
    assert "Perspektiv-Anweisungen" in res.text
    assert "Anwenden" in res.text
