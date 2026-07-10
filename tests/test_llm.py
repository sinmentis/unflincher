import pytest

import diary.llm as llm_module
from diary.llm import chat_reply, generate_commentary, generate_report, generate_session_title


class _FakeStream:
    """Replaces diary.llm.stream_completion in tests — yields fixed tokens and records
    the (system, user_content, model) it was called with, so tests can assert on prompt
    assembly without a real network call."""
    def __init__(self, tokens):
        self.tokens = tokens
        self.calls = []

    async def __call__(self, system, user_content, model):
        self.calls.append((system, user_content, model))
        for t in self.tokens:
            yield t


@pytest.fixture
def fake_stream(monkeypatch):
    fake = _FakeStream(["你", "好"])
    monkeypatch.setattr(llm_module, "stream_completion", fake)
    return fake


ENTRIES = [
    {"id": 1, "title": "第一篇", "entry_date": "2020-01-01", "content_text": "内容一"},
    {"id": 2, "title": "第二篇", "entry_date": "2020-02-01", "content_text": "内容二"},
]


async def test_generate_commentary_streams_and_never_writes_db(fake_stream):
    tokens = [t async for t in generate_commentary(ENTRIES[0], ENTRIES, "人设", "test-model")]
    assert tokens == ["你", "好"]

    system, user_content, model = fake_stream.calls[0]
    assert "人设" in system
    assert model == "test-model"
    # both entries are in context, not just the target one (rubber-duck fix: same context
    # for real generation and test-run preview)
    assert "第一篇" in user_content
    assert "第二篇" in user_content


async def test_generate_commentary_for_second_entry_still_sees_full_corpus(fake_stream):
    async for _ in generate_commentary(ENTRIES[1], ENTRIES, "人设", "test-model"):
        pass
    _, user_content, _ = fake_stream.calls[0]
    assert "第一篇" in user_content
    assert "第二篇" in user_content


async def test_generate_report_includes_all_entries(fake_stream):
    async for _ in generate_report(ENTRIES, "人设", "test-model"):
        pass
    _, user_content, _ = fake_stream.calls[0]
    assert "第一篇" in user_content and "第二篇" in user_content


async def test_chat_reply_includes_history_and_latest_commentary(fake_stream):
    history = [{"role": "user", "content": "之前问过"}, {"role": "assistant", "content": "之前答过"}]
    async for _ in chat_reply(
        ENTRIES[0], "上次锐评内容", history, "新问题", "人设", "test-model"
    ):
        pass
    system, user_content, _ = fake_stream.calls[0]
    assert "上次锐评内容" in system or "上次锐评内容" in user_content
    assert "之前问过" in user_content
    assert "新问题" in user_content


async def _fake_title_tokens(system, user_content, model):
    for t in ["该", "不", "该", "辞职"]:
        yield t


async def test_generate_session_title_joins_stream_and_strips(monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion", _fake_title_tokens)
    title = await generate_session_title("我是不是该辞职", "gpt-5.4-mini")
    assert title == "该不该辞职"


async def test_generate_session_title_passes_the_requested_model(monkeypatch):
    seen = {}

    async def _capture(system, user_content, model):
        seen["model"] = model
        yield "x"

    monkeypatch.setattr(llm_module, "stream_completion", _capture)
    await generate_session_title("随便什么", "gpt-5.4-mini")
    assert seen["model"] == "gpt-5.4-mini"
