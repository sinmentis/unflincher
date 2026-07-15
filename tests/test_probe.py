"""Tests for the local-only synthetic deployment probe (see probe.py's module docstring). It
must never touch the database, must build its request through the shared envelope/preflight
seam, and must make exactly one real completion call."""
import inspect

import pytest

import unflincher.llm as llm_module
import unflincher.probe as probe_module
from unflincher.context_budget import ContextTooLargeError, ModelLimitsUnavailableError
from unflincher.probe import run_probe
from unflincher.request_envelope import build_envelope


def test_probe_module_never_imports_the_database_layer():
    # Structural regression guard: the probe is one of exactly two allowed maintenance bypasses
    # specifically BECAUSE it makes no database writes at all. If this module ever starts
    # importing unflincher.db, that guarantee is broken. Inspect actual import statements (not
    # prose in the docstring, which legitimately mentions unflincher.db by name).
    import ast

    tree = ast.parse(inspect.getsource(probe_module))
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any(m == "unflincher.db" or m.startswith("unflincher.db.") for m in imported_modules)
    assert not hasattr(probe_module, "get_connection")


class _FakeLimits:
    def __init__(self, max_prompt_tokens):
        self.max_prompt_tokens = max_prompt_tokens


class _FakeCapabilities:
    def __init__(self, max_prompt_tokens):
        self.limits = _FakeLimits(max_prompt_tokens)


class _FakeModelInfo:
    def __init__(self, id, name, max_prompt_tokens):
        self.id = id
        self.name = name
        self.capabilities = _FakeCapabilities(max_prompt_tokens)


class _FakeSession:
    def __init__(self, reply_tokens, session_id="probe-session"):
        self._reply_tokens = reply_tokens
        self.session_id = session_id
        self._handler = None

    def on(self, handler):
        self._handler = handler
        return lambda: None

    async def send(self, content):
        from copilot.generated.session_events import AssistantIdleData, AssistantMessageDeltaData

        for tok in self._reply_tokens:
            self._handler(type("Evt", (), {"data": AssistantMessageDeltaData(delta_content=tok, message_id="m1")})())
        self._handler(type("Evt", (), {"data": AssistantIdleData()})())


class _FakeClient:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0
        self.create_session_kwargs = None
        self.models_to_return = []
        self.deleted_session_ids = []

    async def start(self):
        self.start_calls += 1

    async def stop(self):
        self.stop_calls += 1

    async def force_stop(self):
        pass

    async def list_models(self):
        return self.models_to_return

    async def create_session(self, **kwargs):
        self.create_session_kwargs = kwargs
        session = _FakeSession(["ok"])
        return session

    async def delete_session(self, session_id):
        self.deleted_session_ids.append(session_id)


@pytest.fixture(autouse=True)
def _reset_llm_state(monkeypatch):
    monkeypatch.setattr(llm_module, "_client", None)
    monkeypatch.setattr(llm_module, "_client_generation", 0)
    monkeypatch.setattr(llm_module, "_active_count", 0)
    monkeypatch.setattr(llm_module, "_refresh_active", False)
    import asyncio
    monkeypatch.setattr(llm_module, "_lifecycle_cond", asyncio.Condition())
    monkeypatch.setattr(llm_module, "_llm_semaphore", asyncio.Semaphore(4))
    yield


async def test_run_probe_returns_the_model_reply(monkeypatch):
    fake = _FakeClient()
    fake.models_to_return = [_FakeModelInfo("claude-sonnet-4.6", "Claude Sonnet 4.6", 200_000)]
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    reply = await run_probe("claude-sonnet-4.6")

    assert reply == "ok"
    assert fake.create_session_kwargs["model"] == "claude-sonnet-4.6"
    assert fake.create_session_kwargs["available_tools"] == []
    assert fake.create_session_kwargs["system_message"]["mode"] == "replace"


async def test_run_probe_raises_context_too_large_for_a_tiny_limit(monkeypatch):
    fake = _FakeClient()
    fake.models_to_return = [_FakeModelInfo("claude-sonnet-4.6", "Claude Sonnet 4.6", 1)]
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    with pytest.raises(ContextTooLargeError):
        await run_probe("claude-sonnet-4.6")
    assert fake.create_session_kwargs is None  # never called the model


async def test_run_probe_raises_model_limits_unavailable_for_unknown_model(monkeypatch):
    fake = _FakeClient()
    fake.models_to_return = [_FakeModelInfo("other-model", "Other", 200_000)]
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    with pytest.raises(ModelLimitsUnavailableError):
        await run_probe("claude-sonnet-4.6")
    assert fake.create_session_kwargs is None


async def test_run_probe_generates_from_the_exact_preflighted_envelope(monkeypatch):
    # The probe must never rebuild a request from strings after preflight -- assert the SAME
    # envelope object flows from build_envelope() through preflight_envelope() into
    # stream_completion_envelope().
    import unflincher.probe as probe_module

    fake = _FakeClient()
    fake.models_to_return = [_FakeModelInfo("claude-sonnet-4.6", "Claude Sonnet 4.6", 200_000)]
    monkeypatch.setattr(llm_module, "CopilotClient", lambda: fake)

    seen_envelopes = []
    original = llm_module.stream_completion_envelope

    async def _spy(envelope):
        seen_envelopes.append(envelope)
        async for token in original(envelope):
            yield token

    monkeypatch.setattr(llm_module, "stream_completion_envelope", _spy)

    built_envelope = build_envelope(
        probe_module.PROBE_SYSTEM_PROMPT, probe_module.PROBE_USER_MESSAGE, "claude-sonnet-4.6",
        target_kind=probe_module.PROBE_TARGET_KIND,
    )

    await run_probe("claude-sonnet-4.6")

    assert len(seen_envelopes) == 1
    # Same CONTENT as what build_envelope would independently produce (proves no drift), and the
    # probe module itself never calls llm.stream_completion (the string-rebuilding wrapper).
    from unflincher.request_envelope import canonical_json
    assert canonical_json(seen_envelopes[0]) == canonical_json(built_envelope)
