import asyncio

import pytest

import diary.llm as llm_module
from diary.llm import chat_reply, generate_commentary, generate_report, generate_session_title
from copilot.generated.session_events import (
    AssistantIdleData,
    AssistantMessageDeltaData,
    SessionErrorData,
)


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


# ---------------------------------------------------------------------------
# Task 2: stream_completion() rewrite tests
# ---------------------------------------------------------------------------

class _FakeEvent:
    def __init__(self, data):
        self.data = data


class _FakeSession:
    """Replays a scripted sequence of events to on_event, then supports .on()/.send() the same
    shape stream_completion() expects. `session_id` is a fixed string per instance so
    delete_session() calls can be asserted against it."""

    def __init__(self, events, session_id="fake-session-1"):
        self._events = events
        self.session_id = session_id
        self._on_event = None

    def on(self, handler):
        self._on_event = handler
        return lambda: None  # unsubscribe callable

    async def send(self, content):
        for event in self._events:
            self._on_event(event)


class _FakeCopilotClientWithSessions(_FakeCopilotClient):
    """Extends the Task 1 fake with create_session()/delete_session() so stream_completion() can
    be driven end-to-end without a real CLI subprocess. `sessions_to_create` is a list consumed
    one-per-create_session() call, in order — lets a test script "first call raises, second call
    succeeds" scenarios."""

    def __init__(self):
        super().__init__()
        self.sessions_to_create: list = []
        self.deleted_session_ids: list[str] = []

    async def create_session(self, **kwargs):
        next_item = self.sessions_to_create.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item

    async def delete_session(self, session_id):
        self.deleted_session_ids.append(session_id)


async def _collect(agen):
    return [token async for token in agen]


async def test_stream_completion_reuses_client_across_two_calls():
    fake = _FakeCopilotClientWithSessions()
    fake.sessions_to_create = [
        _FakeSession([_FakeEvent(AssistantMessageDeltaData(delta_content="A", message_id="m1")), _FakeEvent(AssistantIdleData())]),
        _FakeSession([_FakeEvent(AssistantMessageDeltaData(delta_content="B", message_id="m1")), _FakeEvent(AssistantIdleData())]),
    ]
    llm_module.CopilotClient = lambda: fake

    result1 = await _collect(llm_module.stream_completion("sys", "msg1", "test-model"))
    result2 = await _collect(llm_module.stream_completion("sys", "msg2", "test-model"))

    assert result1 == ["A"]
    assert result2 == ["B"]
    assert fake.start_calls == 1  # only started once across both calls
    assert fake.stop_calls == 0   # never stopped per-call anymore


async def test_stream_completion_deletes_session_after_success():
    fake = _FakeCopilotClientWithSessions()
    session = _FakeSession(
        [_FakeEvent(AssistantMessageDeltaData(delta_content="hi", message_id="m1")), _FakeEvent(AssistantIdleData())],
        session_id="session-abc",
    )
    fake.sessions_to_create = [session]
    llm_module.CopilotClient = lambda: fake

    await _collect(llm_module.stream_completion("sys", "msg", "test-model"))

    assert fake.deleted_session_ids == ["session-abc"]


async def test_stream_completion_retries_once_on_transport_failure_before_any_token():
    from copilot.client import ProcessExitedError

    fake = _FakeCopilotClientWithSessions()
    fake.sessions_to_create = [
        ProcessExitedError("CLI died"),
        _FakeSession([_FakeEvent(AssistantMessageDeltaData(delta_content="recovered", message_id="m1")), _FakeEvent(AssistantIdleData())]),
    ]
    llm_module.CopilotClient = lambda: fake

    result = await _collect(llm_module.stream_completion("sys", "msg", "test-model"))

    assert result == ["recovered"]
    # The first (broken) client was torn down and a fresh one started for the retry.
    assert fake.stop_calls == 1


async def test_stream_completion_does_not_retry_after_a_token_was_already_yielded(monkeypatch):
    # A session that yields one token, then stalls forever (no idle/error event follows).
    # After a very short timeout, TransportStalledError fires — this is a transport failure
    # but it arrives AFTER partial was already yielded, so no retry should occur.
    fake = _FakeCopilotClientWithSessions()
    fake.sessions_to_create = [
        _FakeSession(
            [_FakeEvent(AssistantMessageDeltaData(delta_content="partial", message_id="m1"))],
            session_id="session-stall",
        ),
    ]
    llm_module.CopilotClient = lambda: fake
    monkeypatch.setattr(llm_module, "_STALL_TIMEOUT_SECONDS", 0.02)

    agen = llm_module.stream_completion("sys", "msg", "test-model")
    tokens = []
    with pytest.raises(llm_module.TransportStalledError):
        async for token in agen:
            tokens.append(token)

    assert tokens == ["partial"]  # the one token that WAS yielded is not lost/duplicated
    assert len(fake.sessions_to_create) == 0  # no retry consumed a second scripted session
    assert fake.stop_calls == 0  # shared client never torn down when failure is after yield


async def test_stream_completion_never_retries_a_model_level_session_error():
    fake = _FakeCopilotClientWithSessions()
    fake.sessions_to_create = [
        _FakeSession([_FakeEvent(SessionErrorData(error_type="model_error", message="invalid model name"))]),
    ]
    llm_module.CopilotClient = lambda: fake

    with pytest.raises(llm_module.ModelSessionError, match="invalid model name"):
        await _collect(llm_module.stream_completion("sys", "msg", "bad-model-name"))

    assert fake.stop_calls == 0  # never torn down for a model-level error
