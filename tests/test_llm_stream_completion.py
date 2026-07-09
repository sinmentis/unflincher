"""Unit tests for diary.llm.stream_completion against a FAKE Copilot SDK.

These never spawn the real Copilot CLI subprocess, touch the network, or need a real
COPILOT_GITHUB_TOKEN. We monkeypatch `copilot.CopilotClient` (the single SDK entry point
stream_completion imports) with a fake that emits a scripted sequence of REAL session
event-data objects, then assert stream_completion translates them into the right yields,
errors, and cleanup. Using the real event-data classes keeps the `isinstance` dispatch in
the implementation honest — only the client/session plumbing is faked."""
import types

import pytest

from copilot.generated.session_events import (
    AssistantIdleData,
    AssistantMessageDeltaData,
    SessionErrorData,
)

from diary.llm import stream_completion


def _event(data):
    """Wrap event-data in the minimal shape stream_completion reads (`event.data`)."""
    return types.SimpleNamespace(data=data)


def _make_fake_copilot(events):
    """Build a fake CopilotClient class scripted to emit `events` (a list of event-data
    objects) after send(), plus a `record` dict capturing what the SUT did to the SDK."""
    record = {
        "create_kwargs": None,
        "started": False,
        "stopped": False,
        "unsubscribed": False,
        "sent": [],
    }

    class _FakeSession:
        def __init__(self):
            # A set, mirroring the real SDK's handler storage, so registering the same
            # handler twice (once via create_session(on_event=), once via the explicit
            # on() call the SUT makes to capture the unsubscribe handle) still fires it
            # exactly once per event.
            self._handlers = set()

        def on(self, handler):
            self._handlers.add(handler)

            def unsubscribe():
                self._handlers.discard(handler)
                record["unsubscribed"] = True

            return unsubscribe

        async def send(self, prompt):
            record["sent"].append(prompt)
            for data in events:
                evt = _event(data)
                for handler in list(self._handlers):
                    handler(evt)
            return "fake-message-id"

    class _FakeClient:
        async def start(self):
            record["started"] = True

        async def create_session(self, **kwargs):
            record["create_kwargs"] = kwargs
            session = _FakeSession()
            on_event = kwargs.get("on_event")
            if on_event is not None:
                session.on(on_event)
            record["session"] = session
            return session

        async def stop(self):
            record["stopped"] = True

    return _FakeClient, record


async def _collect(system="人设", user="问题", model="test-model"):
    return [token async for token in stream_completion(system, user, model)]


async def test_streams_deltas_in_order_then_ends(monkeypatch):
    FakeClient, record = _make_fake_copilot([
        AssistantMessageDeltaData(delta_content="你", message_id="m1"),
        AssistantMessageDeltaData(delta_content="好", message_id="m1"),
        AssistantIdleData(),
    ])
    monkeypatch.setattr("copilot.CopilotClient", FakeClient)

    tokens = await _collect(user="问题")

    # Deltas yielded in order, generator ends cleanly on idle (no duplicate output from the
    # deliberate double registration, no hang).
    assert tokens == ["你", "好"]
    assert record["started"] is True
    assert record["stopped"] is True
    assert record["unsubscribed"] is True
    assert record["sent"] == ["问题"]


async def test_empty_deltas_are_skipped(monkeypatch):
    FakeClient, _ = _make_fake_copilot([
        AssistantMessageDeltaData(delta_content="A", message_id="m1"),
        AssistantMessageDeltaData(delta_content="", message_id="m1"),
        AssistantMessageDeltaData(delta_content="B", message_id="m1"),
        AssistantIdleData(),
    ])
    monkeypatch.setattr("copilot.CopilotClient", FakeClient)
    assert await _collect() == ["A", "B"]


async def test_model_passed_through_to_create_session(monkeypatch):
    FakeClient, record = _make_fake_copilot([AssistantIdleData()])
    monkeypatch.setattr("copilot.CopilotClient", FakeClient)
    await _collect(model="claude-sonnet-4.6")
    assert record["create_kwargs"]["model"] == "claude-sonnet-4.6"


async def test_system_message_replace_mode_passed(monkeypatch):
    # Locks in the anti-contamination behavior: diary's own persona text replaces the SDK's
    # default coding-agent system prompt.
    FakeClient, record = _make_fake_copilot([AssistantIdleData()])
    monkeypatch.setattr("copilot.CopilotClient", FakeClient)
    await _collect(system="扮演人生导师")
    assert record["create_kwargs"]["system_message"] == {
        "mode": "replace",
        "content": "扮演人生导师",
    }


async def test_available_tools_empty_disables_all_tools(monkeypatch):
    # Locks in the "no tool access" security property.
    FakeClient, record = _make_fake_copilot([AssistantIdleData()])
    monkeypatch.setattr("copilot.CopilotClient", FakeClient)
    await _collect()
    assert record["create_kwargs"]["available_tools"] == []


async def test_session_error_raises_runtimeerror_with_message(monkeypatch):
    FakeClient, _ = _make_fake_copilot([
        SessionErrorData(error_type="model_error", message="rate limited"),
    ])
    monkeypatch.setattr("copilot.CopilotClient", FakeClient)
    with pytest.raises(RuntimeError, match="rate limited"):
        await _collect()


async def test_client_stopped_when_error_raises_midstream(monkeypatch):
    FakeClient, record = _make_fake_copilot([
        AssistantMessageDeltaData(delta_content="partial", message_id="m1"),
        SessionErrorData(error_type="model_error", message="boom"),
    ])
    monkeypatch.setattr("copilot.CopilotClient", FakeClient)
    with pytest.raises(RuntimeError, match="boom"):
        await _collect()
    # Cleanup still runs even though the SessionErrorData path raised mid-stream.
    assert record["stopped"] is True
    assert record["unsubscribed"] is True
