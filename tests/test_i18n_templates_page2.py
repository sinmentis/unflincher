def test_report_page_translates(client):
    client.cookies.set("unflincher_lang", "ko")
    res = client.get("/report")
    assert "아직 생성된 리포트가 없습니다." in res.text
    assert "인생 리포트" in res.text


def test_new_entry_page_translates(client):
    client.cookies.set("unflincher_lang", "es")
    res = client.get("/new")
    assert "Escribir" in res.text
    assert "Guardar entrada" in res.text


def test_chat_list_page_translates(client):
    client.cookies.set("unflincher_lang", "pt")
    res = client.get("/chat")
    assert "Conversas" in res.text
    assert "primeira" in res.text


def test_chat_session_page_translates(client):
    client.cookies.set("unflincher_lang", "ru")
    res = client.get("/chat/new")
    assert "Новый диалог" in res.text
    assert "Отправить" in res.text
