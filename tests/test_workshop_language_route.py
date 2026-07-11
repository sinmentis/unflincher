def test_set_language_sets_cookie_and_returns_ok(client):
    res = client.post("/workshop/language", json={"lang": "zh-Hans"})
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert client.cookies.get("diary_lang") == "zh-Hans"


def test_set_language_rejects_unsupported_code(client):
    res = client.post("/workshop/language", json={"lang": "klingon"})
    assert res.status_code == 400


def test_set_language_persists_across_subsequent_pages(client):
    client.post("/workshop/language", json={"lang": "ja"})
    res = client.get("/")
    assert "タイムライン" in res.text
    assert "人生レポート" in res.text
