import asyncio

import pytest

import diary.llm as llm_module
from diary.llm import chat_reply, generate_commentary, generate_report, generate_session_title


class _FakeCopilotClient:
    """Records start()/stop()/force_stop() calls; every test instance is independent so counts
    never leak between tests. Substituted for the real copilot.CopilotClient via monkeypatch."""
    instances: list["_FakeCopilotClient"] = []

    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0
        self.force_stop_calls = 0
        self.start_should_raise: Exception | None = None
        _FakeCopilotClient.instances.append(self)

    async def start(self):
        self.start_calls += 1
        if self.start_should_raise is not None:
            raise self.start_should_raise

    async def stop(self):
        self.stop_calls += 1

    async def force_stop(self):
        self.force_stop_calls += 1


@pytest.fixture(autouse=True)
def _reset_shared_client_state(monkeypatch):
    """Every test in this file gets a clean shared-client singleton and a fresh fake-instance
    list, regardless of test order or a previous test's failure leaving state dirty."""
    monkeypatch.setattr(llm_module, "_client", None)
    monkeypatch.setattr(llm_module, "_client_generation", 0)
    monkeypatch.setattr(llm_module, "_client_lock", asyncio.Lock())
    _FakeCopilotClient.instances = []
    monkeypatch.setattr(llm_module, "CopilotClient", _FakeCopilotClient)
    yield


async def test_ensure_client_starts_once_and_reuses_across_calls():
    client1, gen1 = await llm_module._ensure_client()
    client2, gen2 = await llm_module._ensure_client()

    assert client1 is client2
    assert gen1 == gen2
    assert client1.start_calls == 1  # not called again on the second _ensure_client()


async def test_ensure_client_bumps_generation_on_each_new_client():
    client1, gen1 = await llm_module._ensure_client()
    await llm_module._stop_shared_client(gen1)
    client2, gen2 = await llm_module._ensure_client()

    assert client2 is not client1
    assert gen2 != gen1


async def test_stop_shared_client_is_noop_if_generation_is_stale():
    # Simulates the ABA race: caller A observed generation g1, but by the time it tries to stop
    # the client, someone else already replaced it with a newer one (generation g2). A's stop
    # call must NOT tear down the newer client.
    client1, gen1 = await llm_module._ensure_client()
    await llm_module._stop_shared_client(gen1)
    client2, gen2 = await llm_module._ensure_client()

    # A's stale stop call, using the OLD generation number, arrives late.
    await llm_module._stop_shared_client(gen1)

    assert client2.stop_calls == 0  # the newer client survives untouched
    current_client, current_gen = await llm_module._ensure_client()
    assert current_client is client2
    assert current_gen == gen2


async def test_ensure_client_force_stops_on_start_failure():
    # Pre-seed a fake instance that will fail to start, by monkeypatching the CopilotClient
    # constructor itself to always return one configured to raise.
    def _make_failing_client():
        c = _FakeCopilotClient()
        c.start_should_raise = RuntimeError("boom")
        return c

    llm_module.CopilotClient = _make_failing_client

    with pytest.raises(RuntimeError, match="boom"):
        await llm_module._ensure_client()

    assert _FakeCopilotClient.instances[-1].force_stop_calls == 1


async def test_warm_up_client_swallows_failure():
    def _make_failing_client():
        c = _FakeCopilotClient()
        c.start_should_raise = RuntimeError("auth not ready yet")
        return c

    llm_module.CopilotClient = _make_failing_client

    await llm_module.warm_up_client()  # must not raise

    # The shared client singleton must be left clean (None), not holding a half-started instance,
    # so the next real request's _ensure_client() call retries from scratch.
    assert llm_module._client is None


async def test_shutdown_client_stops_the_current_client():
    client, gen = await llm_module._ensure_client()
    await llm_module.shutdown_client()

    assert client.stop_calls == 1
    assert llm_module._client is None


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
