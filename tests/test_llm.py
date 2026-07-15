import asyncio

import pytest

import unflincher.llm as llm_module
from unflincher.llm import chat_reply, generate_commentary, generate_report, generate_session_title
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
    """Every test in this file gets a clean shared-client lifecycle state and a fresh
    fake-instance list, regardless of test order or a previous test's failure leaving state
    dirty."""
    monkeypatch.setattr(llm_module, "_client", None)
    monkeypatch.setattr(llm_module, "_client_generation", 0)
    monkeypatch.setattr(llm_module, "_active_count", 0)
    monkeypatch.setattr(llm_module, "_refresh_active", False)
    monkeypatch.setattr(llm_module, "_lifecycle_cond", asyncio.Condition())
    monkeypatch.setattr(llm_module, "_llm_semaphore", asyncio.Semaphore(4))
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
    """Replaces unflincher.llm.stream_completion in tests — yields fixed tokens and records
    the (system, user_content, model) it was called with, so tests can assert on prompt
    assembly without a real network call. Accepts (and ignores) the target_kind/target_id
    keyword-only params every real generate_*/chat_* wrapper now passes through."""
    def __init__(self, tokens):
        self.tokens = tokens
        self.calls = []

    async def __call__(self, system, user_content, model, *, target_kind=None, target_id=None):
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


async def _fake_title_tokens(system, user_content, model, *, target_kind=None, target_id=None):
    for t in ["该", "不", "该", "辞职"]:
        yield t


async def test_generate_session_title_joins_stream_and_strips(monkeypatch):
    monkeypatch.setattr(llm_module, "stream_completion", _fake_title_tokens)
    title = await generate_session_title("我是不是该辞职", "gpt-5.4-mini")
    assert title == "该不该辞职"


async def test_generate_session_title_passes_the_requested_model(monkeypatch):
    seen = {}

    async def _capture(system, user_content, model, *, target_kind=None, target_id=None):
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


async def test_stream_completion_does_not_retry_or_swallow_a_domain_safety_error():
    # Regression guard for the "broad RuntimeError transport handling can swallow stable
    # domain/safety errors" class of bug: every stable domain error in this app (maintenance,
    # lease, archive, context-budget) is itself a RuntimeError subclass. If stream_completion's
    # transport-retry except clause ever catches a bare RuntimeError again, an error like this one
    # raised mid-stream would be silently retried/misclassified as a transport hiccup instead of
    # propagating immediately.
    from unflincher.db import MaintenanceLockedError

    class _RaisingSession(_FakeSession):
        async def send(self, content):
            raise MaintenanceLockedError("maintenance locked mid-stream")

    fake = _FakeCopilotClientWithSessions()
    fake.sessions_to_create = [_RaisingSession([], session_id="session-x")]
    llm_module.CopilotClient = lambda: fake

    with pytest.raises(MaintenanceLockedError):
        await _collect(llm_module.stream_completion("sys", "msg", "test-model"))

    # Not treated as a retryable transport failure: no reset-and-retry occurred.
    assert fake.stop_calls == 0
    assert len(fake.sessions_to_create) == 0  # the one scripted session was consumed, not retried


# ---------------------------------------------------------------------------
# Prepared-request interface: preflight and generation share the EXACT envelope object
# ---------------------------------------------------------------------------

async def test_generate_from_prepared_streams_the_exact_preflighted_envelope_object(monkeypatch):
    seen_envelopes = []

    async def _fake_stream_completion_envelope(envelope):
        seen_envelopes.append(envelope)
        yield "token"

    monkeypatch.setattr(llm_module, "stream_completion_envelope", _fake_stream_completion_envelope)
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _AsyncReturn(200_000))

    entry = ENTRIES[0]
    prepared = await llm_module.prepare_commentary_request(entry, ENTRIES, "人设", "test-model")
    tokens = [t async for t in llm_module.generate_from_prepared(prepared)]

    assert tokens == ["token"]
    assert len(seen_envelopes) == 1
    # Identity, not just equality: generate_from_prepared must pass the SAME object that was
    # preflighted, never rebuild a new one from strings.
    assert seen_envelopes[0] is prepared.envelope


class _AsyncReturn:
    """Callable returning a coroutine that resolves to a fixed value -- used to stub
    get_model_max_prompt_tokens() without needing a fake Copilot client for pure envelope tests."""
    def __init__(self, value):
        self.value = value

    async def __call__(self, model):
        return self.value


async def test_prepare_commentary_request_raises_context_too_large_before_any_model_call(monkeypatch):
    from unflincher.context_budget import ContextTooLargeError

    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _AsyncReturn(1))
    with pytest.raises(ContextTooLargeError) as excinfo:
        await llm_module.prepare_commentary_request(ENTRIES[0], ENTRIES, "人设", "test-model")
    assert excinfo.value.target_kind == "entry_commentary"
    assert str(ENTRIES[0]["id"]) == excinfo.value.target_id


async def test_prepare_report_request_raises_model_limits_unavailable(monkeypatch):
    from unflincher.context_budget import ModelLimitsUnavailableError

    async def _raise(model):
        raise ModelLimitsUnavailableError(model, "boom")

    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _raise)
    with pytest.raises(ModelLimitsUnavailableError):
        await llm_module.prepare_report_request(ENTRIES, "人设", "test-model")


async def test_build_commentary_envelope_and_prepare_commentary_request_agree_on_content():
    # The envelope built by the pure builder (used for batch preflight, e.g. apply-all) and the
    # one produced by the one-shot prepare_*_request path must be byte-for-byte identical for the
    # same inputs -- content assembly must never fork between the two call styles.
    from unflincher.request_envelope import canonical_json

    pure = llm_module.build_commentary_envelope(ENTRIES[0], ENTRIES, "人设", "test-model")

    async def _fake_limit(model):
        return 200_000

    import unflincher.llm as module
    orig = module.get_model_max_prompt_tokens
    module.get_model_max_prompt_tokens = _fake_limit
    try:
        prepared = await llm_module.prepare_commentary_request(ENTRIES[0], ENTRIES, "人设", "test-model")
    finally:
        module.get_model_max_prompt_tokens = orig

    assert canonical_json(pure) == canonical_json(prepared.envelope)


# ---------------------------------------------------------------------------
# Shared Copilot client lifecycle: race-free admission/replacement/refresh
# ---------------------------------------------------------------------------

async def test_stop_shared_client_waits_for_other_active_generation_before_tearing_down():
    # Simulates the exact race the plan fixes: stream A observed generation g1, failed, and
    # (per stream_completion's contract) already released its OWN admission before asking to
    # replace g1. Stream B is a DIFFERENT, still-active generation on the SAME client/generation.
    # Replacement must wait for B to finish, never yank the client out from under it.
    fake = _FakeCopilotClient()
    llm_module.CopilotClient = lambda: fake

    client_a, gen_a = await llm_module._admit_generation()
    client_b, gen_b = await llm_module._admit_generation()
    assert client_a is client_b
    assert gen_a == gen_b
    await llm_module._release_generation()  # A releases its own admission first

    order: list[str] = []

    async def _replace():
        order.append("replace-start")
        await llm_module._stop_shared_client(gen_a)
        order.append("replace-end")

    replace_task = asyncio.create_task(_replace())
    await asyncio.sleep(0.02)
    # B is still holding its admission — replacement must still be blocked.
    assert "replace-end" not in order
    assert fake.stop_calls == 0

    await llm_module._release_generation()  # B releases
    await replace_task

    assert order == ["replace-start", "replace-end"]
    assert fake.stop_calls == 1  # torn down only after every active generation released


async def test_new_admission_waits_for_in_progress_replacement_to_finish():
    order: list[str] = []
    fake = _FakeCopilotClient()

    async def _slow_stop():
        order.append("stop-start")
        await asyncio.sleep(0.03)
        order.append("stop-end")

    fake.stop = _slow_stop
    llm_module.CopilotClient = lambda: fake

    client_a, gen_a = await llm_module._admit_generation()
    await llm_module._release_generation()

    replace_task = asyncio.create_task(llm_module._stop_shared_client(gen_a))
    await asyncio.sleep(0.005)  # let replacement claim the transition and start tearing down

    async def _new_admission():
        order.append("admission-call")
        client, gen = await llm_module._admit_generation()
        order.append("admission-done")
        return client, gen

    admission_task = asyncio.create_task(_new_admission())
    await replace_task
    new_client, new_gen = await admission_task
    await llm_module._release_generation()

    assert order.index("stop-end") < order.index("admission-done")
    assert new_gen != gen_a  # a genuinely new generation, proving replacement actually happened
    assert fake.start_calls == 2  # started once initially, once again for the replacement


async def test_stop_shared_client_is_aba_safe_for_two_concurrent_failures():
    # Two different streams both observed the same (now-stale) generation and both concluded it
    # failed. Only the first actually tears the client down; the second is a no-op — this is the
    # ABA-race guard the module-level docstring describes.
    fake = _FakeCopilotClient()
    llm_module.CopilotClient = lambda: fake
    client_a, gen_a = await llm_module._admit_generation()
    await llm_module._release_generation()

    await asyncio.gather(
        llm_module._stop_shared_client(gen_a),
        llm_module._stop_shared_client(gen_a),
    )
    assert fake.stop_calls == 1


async def test_transport_failure_does_not_kill_a_concurrent_peer_stream_end_to_end():
    # Full stream_completion() integration: stream A fails with a transport error before yielding
    # any token and retries; stream B is a genuinely concurrent, already-admitted generation on
    # the ORIGINAL client. B must complete successfully and must never observe its session/client
    # torn out from under it.
    from copilot.client import ProcessExitedError

    fake = _FakeCopilotClientWithSessions()
    b_started = asyncio.Event()
    b_may_finish = asyncio.Event()

    class _BSession(_FakeSession):
        async def send(self, content):
            b_started.set()
            await b_may_finish.wait()
            await super().send(content)

    fake.sessions_to_create = [
        _BSession(
            [_FakeEvent(AssistantMessageDeltaData(delta_content="b-token", message_id="mb")), _FakeEvent(AssistantIdleData())],
            session_id="session-b",
        ),
        ProcessExitedError("A's first attempt crashed"),
        _FakeSession(
            [_FakeEvent(AssistantMessageDeltaData(delta_content="a-recovered", message_id="ma")), _FakeEvent(AssistantIdleData())],
            session_id="session-a-retry",
        ),
    ]
    llm_module.CopilotClient = lambda: fake

    async def _stream_b():
        return await _collect(llm_module.stream_completion("sys", "b-msg", "test-model"))

    b_task = asyncio.create_task(_stream_b())
    await b_started.wait()  # B is now admitted and mid-stream on the original client/generation

    async def _stream_a():
        return await _collect(llm_module.stream_completion("sys", "a-msg", "test-model"))

    # A starts AFTER B is already active: A's create_session() raises immediately (no admission
    # race to win), so A's failure-and-replace path begins concurrently with B's in-flight
    # stream. Because B is still holding its admission, A's replacement request must BLOCK until
    # B finishes -- proving it cannot terminate B's still-active client out from under it.
    a_task = asyncio.create_task(_stream_a())
    await asyncio.sleep(0.01)
    b_may_finish.set()
    b_result = await b_task
    a_result = await a_task

    assert a_result == ["a-recovered"]
    assert b_result == ["b-token"]  # B's stream was never interrupted by A's replacement
    assert fake.deleted_session_ids == ["session-b", "session-a-retry"]


# ---------------------------------------------------------------------------
# Task 3: Concurrency limit tests
# ---------------------------------------------------------------------------

def test_settings_default_llm_concurrency(monkeypatch):
    from unflincher.config import load_settings
    monkeypatch.delenv("UNFLINCHER_LLM_CONCURRENCY", raising=False)
    settings = load_settings()
    assert settings.llm_concurrency == 4


def test_settings_llm_concurrency_from_env(monkeypatch):
    from unflincher.config import load_settings
    monkeypatch.setenv("UNFLINCHER_LLM_CONCURRENCY", "7")
    settings = load_settings()
    assert settings.llm_concurrency == 7


def test_settings_image_identity_from_env(monkeypatch):
    from unflincher.config import load_settings

    monkeypatch.setenv("UNFLINCHER_REVISION", "a" * 40)
    monkeypatch.setenv("UNFLINCHER_VERSION", "0.2.0")

    settings = load_settings()

    assert settings.revision == "a" * 40
    assert settings.version == "0.2.0"


async def test_stream_completion_limits_concurrent_sessions(monkeypatch):
    # Reconfigure the module's semaphore to a limit of 1 for this test, then launch two
    # stream_completion() calls concurrently and verify the second one's session isn't created
    # until the first has released the semaphore (proven by an ordering list both fakes append
    # to).
    monkeypatch.setattr(llm_module, "_llm_semaphore", asyncio.Semaphore(1))
    order: list[str] = []

    class _OrderedSession(_FakeSession):
        def __init__(self, label, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.label = label

        async def send(self, content):
            order.append(f"start-{self.label}")
            await asyncio.sleep(0.01)
            order.append(f"end-{self.label}")
            await super().send(content)

    fake = _FakeCopilotClientWithSessions()
    fake.sessions_to_create = [
        _OrderedSession("A", [_FakeEvent(AssistantMessageDeltaData(delta_content="a", message_id="m1")), _FakeEvent(AssistantIdleData())]),
        _OrderedSession("B", [_FakeEvent(AssistantMessageDeltaData(delta_content="b", message_id="m1")), _FakeEvent(AssistantIdleData())]),
    ]
    llm_module.CopilotClient = lambda: fake

    await asyncio.gather(
        _collect(llm_module.stream_completion("sys", "msg-a", "test-model")),
        _collect(llm_module.stream_completion("sys", "msg-b", "test-model")),
    )

    # With a semaphore of 1, B cannot start until A has fully finished (start-A, end-A, start-B,
    # end-B) — never interleaved (start-A, start-B, end-A, end-B).
    assert order == ["start-A", "end-A", "start-B", "end-B"]


# ---------------------------------------------------------------------------
# Task 4: Live model list tests
# ---------------------------------------------------------------------------

class _ModelInfo:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class _FakeCopilotClientWithModels(_FakeCopilotClientWithSessions):
    def __init__(self):
        super().__init__()
        self.list_models_calls = 0
        self.models_to_return: list[_ModelInfo] = []

    async def list_models(self):
        self.list_models_calls += 1
        return self.models_to_return


async def test_list_available_models_filters_out_auto():
    fake = _FakeCopilotClientWithModels()
    fake.models_to_return = [
        _ModelInfo("auto", "Auto"),
        _ModelInfo("gpt-5.5", "GPT-5.5"),
        _ModelInfo("claude-opus-4.8", "Claude Opus 4.8"),
    ]
    llm_module.CopilotClient = lambda: fake

    result = await llm_module.list_available_models()

    assert result == [("gpt-5.5", "GPT-5.5"), ("claude-opus-4.8", "Claude Opus 4.8")]


# ---------------------------------------------------------------------------
# Context budget: model max_prompt_tokens lookup
# ---------------------------------------------------------------------------

class _FakeLimits:
    def __init__(self, max_prompt_tokens):
        self.max_prompt_tokens = max_prompt_tokens


class _FakeCapabilities:
    def __init__(self, max_prompt_tokens):
        self.limits = _FakeLimits(max_prompt_tokens)


class _ModelInfoWithCapabilities(_ModelInfo):
    def __init__(self, id, name, max_prompt_tokens):
        super().__init__(id, name)
        self.capabilities = _FakeCapabilities(max_prompt_tokens)


async def test_get_model_max_prompt_tokens_returns_the_published_limit():
    fake = _FakeCopilotClientWithModels()
    fake.models_to_return = [
        _ModelInfoWithCapabilities("claude-sonnet-4.6", "Claude Sonnet 4.6", 200_000),
    ]
    llm_module.CopilotClient = lambda: fake

    limit = await llm_module.get_model_max_prompt_tokens("claude-sonnet-4.6")

    assert limit == 200_000


async def test_get_model_max_prompt_tokens_raises_when_model_not_in_catalog():
    from unflincher.context_budget import ModelLimitsUnavailableError

    fake = _FakeCopilotClientWithModels()
    fake.models_to_return = [_ModelInfoWithCapabilities("gpt-5.5", "GPT-5.5", 100_000)]
    llm_module.CopilotClient = lambda: fake

    with pytest.raises(ModelLimitsUnavailableError):
        await llm_module.get_model_max_prompt_tokens("nonexistent-model")


async def test_get_model_max_prompt_tokens_raises_when_limit_missing():
    from unflincher.context_budget import ModelLimitsUnavailableError

    fake = _FakeCopilotClientWithModels()
    fake.models_to_return = [_ModelInfoWithCapabilities("claude-sonnet-4.6", "Claude Sonnet 4.6", None)]
    llm_module.CopilotClient = lambda: fake

    with pytest.raises(ModelLimitsUnavailableError):
        await llm_module.get_model_max_prompt_tokens("claude-sonnet-4.6")


async def test_get_model_max_prompt_tokens_raises_when_model_list_fetch_fails():
    from unflincher.context_budget import ModelLimitsUnavailableError

    class _BrokenListModels(_FakeCopilotClientWithModels):
        async def list_models(self):
            raise RuntimeError("copilot CLI unreachable")

    fake = _BrokenListModels()
    llm_module.CopilotClient = lambda: fake

    with pytest.raises(ModelLimitsUnavailableError):
        await llm_module.get_model_max_prompt_tokens("claude-sonnet-4.6")


async def test_validate_selected_model_accepts_active_model_without_catalog_call():
    """The currently active model must never require a catalog round trip -- a temporary
    model-list outage must not block continuing to use the model already in production."""
    class _BrokenListModels(_FakeCopilotClientWithModels):
        async def list_models(self):
            raise RuntimeError("copilot CLI unreachable")

    fake = _BrokenListModels()
    llm_module.CopilotClient = lambda: fake

    await llm_module.validate_selected_model("claude-sonnet-4.6", "claude-sonnet-4.6")

    assert fake.list_models_calls == 0


async def test_validate_selected_model_accepts_a_changed_model_present_in_catalog():
    fake = _FakeCopilotClientWithModels()
    fake.models_to_return = [_ModelInfo("gpt-5.5", "GPT-5.5")]
    llm_module.CopilotClient = lambda: fake

    await llm_module.validate_selected_model("gpt-5.5", "claude-sonnet-4.6")


async def test_validate_selected_model_raises_unsupported_for_a_changed_model_not_in_catalog():
    from unflincher.llm import UnsupportedModelError

    fake = _FakeCopilotClientWithModels()
    fake.models_to_return = [_ModelInfo("gpt-5.5", "GPT-5.5")]
    llm_module.CopilotClient = lambda: fake

    with pytest.raises(UnsupportedModelError) as excinfo:
        await llm_module.validate_selected_model("nonexistent-model", "claude-sonnet-4.6")
    assert excinfo.value.model == "nonexistent-model"


async def test_validate_selected_model_raises_retryable_when_catalog_unavailable():
    from unflincher.context_budget import ModelLimitsUnavailableError

    class _BrokenListModels(_FakeCopilotClientWithModels):
        async def list_models(self):
            raise RuntimeError("copilot CLI unreachable")

    fake = _BrokenListModels()
    llm_module.CopilotClient = lambda: fake

    with pytest.raises(ModelLimitsUnavailableError):
        await llm_module.validate_selected_model("gpt-5.5", "claude-sonnet-4.6")


async def test_refresh_available_models_restarts_client_and_refetches():
    fake = _FakeCopilotClientWithModels()
    fake.models_to_return = [_ModelInfo("gpt-5.5", "GPT-5.5")]
    llm_module.CopilotClient = lambda: fake

    await llm_module.list_available_models()  # first fetch, populates the SDK's own cache
    result = await llm_module.refresh_available_models()

    assert result == [("gpt-5.5", "GPT-5.5")]
    assert fake.stop_calls == 1  # the old client was torn down to bust its cache
    assert fake.start_calls == 2  # a fresh client was started for the refetch


async def test_refresh_available_models_waits_for_active_generation_to_finish():
    # Plan-mandated behaviour change: refresh must WAIT for an active generation to finish
    # rather than instantly refusing (the old "正在生成中" instant-reject). It must also not tear
    # the client down (or start a new one) until the generation has actually released its
    # admission — proven by an ordering list both the stream and the refresh append to.
    order: list[str] = []

    class _SlowSession(_FakeSession):
        async def send(self, content):
            order.append("stream-start")
            await asyncio.sleep(0.03)
            order.append("stream-end")
            await super().send(content)

    fake = _FakeCopilotClientWithModels()
    fake.sessions_to_create = [
        _SlowSession([_FakeEvent(AssistantMessageDeltaData(delta_content="x", message_id="m1")), _FakeEvent(AssistantIdleData())]),
    ]
    fake.models_to_return = [_ModelInfo("gpt-5.5", "GPT-5.5")]
    llm_module.CopilotClient = lambda: fake

    async def _stream():
        async for _ in llm_module.stream_completion("sys", "msg", "test-model"):
            pass

    async def _refresh():
        # Give the stream a moment to actually start (enter _admit_generation) before refresh
        # begins its transition, so refresh genuinely observes an active generation to wait for.
        await asyncio.sleep(0.01)
        order.append("refresh-start")
        result = await llm_module.refresh_available_models()
        order.append("refresh-end")
        return result

    stream_task = asyncio.create_task(_stream())
    refresh_task = asyncio.create_task(_refresh())
    result = await refresh_task
    await stream_task

    assert result == [("gpt-5.5", "GPT-5.5")]
    # refresh started while the stream was active but did not finish (tear down/restart the
    # client) until AFTER the stream released its admission.
    assert order.index("refresh-start") < order.index("stream-end")
    assert order.index("stream-end") < order.index("refresh-end")
    assert fake.stop_calls == 1
    assert fake.start_calls == 2


async def test_refresh_available_models_blocks_new_admission_until_it_completes():
    # A generation attempted WHILE refresh is transitioning must wait for the transition to
    # finish before it can even start streaming (new admissions are blocked, not merely delayed
    # arbitrarily) — proven by asserting the new stream's session is only created after refresh
    # has installed its new client.
    order: list[str] = []
    fake = _FakeCopilotClientWithModels()
    fake.models_to_return = []

    async def _slow_start(self):
        order.append("refresh-client-start-begin")
        await asyncio.sleep(0.03)
        order.append("refresh-client-start-end")

    fake.start = _slow_start.__get__(fake)
    fake.sessions_to_create = [
        _FakeSession([_FakeEvent(AssistantMessageDeltaData(delta_content="y", message_id="m1")), _FakeEvent(AssistantIdleData())]),
    ]
    llm_module.CopilotClient = lambda: fake

    async def _refresh():
        order.append("refresh-call")
        await llm_module.refresh_available_models()

    async def _new_admission():
        await asyncio.sleep(0.005)  # let refresh claim the transition first
        order.append("admission-call")
        async for _ in llm_module.stream_completion("sys", "msg", "test-model"):
            pass
        order.append("admission-done")

    await asyncio.gather(_refresh(), _new_admission())

    # The new admission's stream must not have proceeded until AFTER refresh finished starting
    # its replacement client.
    assert order.index("refresh-client-start-end") < order.index("admission-done")
