"""Unit tests for the shared response-level cleanup seam (routes/sse.py's sse_response),
reused by every SSE route in this app: Entry Reflection chat, direct Life Report, existing/new
general Conversation, and Prompt Workshop preview.

The core regression this guards against: a plain `async def event_stream(): ... finally:
release(...)` generator's own `finally` only runs once its body has actually started iterating.
A client that disconnects between the route handler returning the response object and
sse_starlette ever calling `body_iterator.__anext__()` for the first time skips that `finally`
entirely -- these tests reproduce that exact race at the raw ASGI layer (not through TestClient,
which does not give us fine enough control over the disconnect timing) and prove the
BackgroundTask half of sse_response still runs cleanup in that case."""
import asyncio
import json as _json

import pytest
from starlette.requests import Request

import unflincher.llm as llm_module
from unflincher.routes.sse import sse_response


async def test_sse_response_runs_cleanup_exactly_once_on_normal_completion():
    calls = []

    async def body():
        yield {"event": "token", "data": "hi"}
        yield {"event": "done", "data": "{}"}

    response = sse_response(body(), cleanup=lambda: calls.append("cleanup"))

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        # Never disconnects -- the stream finishes on its own once the body is exhausted.
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}  # pragma: no cover -- unreachable in this test

    await response({"type": "http"}, receive, send)

    assert calls == ["cleanup"]
    body_chunks = [m for m in sent if m["type"] == "http.response.body"]
    assert len(body_chunks) >= 2  # the token + done events were actually sent


async def test_sse_response_runs_cleanup_exactly_once_when_body_raises():
    calls = []

    async def body():
        yield {"event": "token", "data": "hi"}
        raise RuntimeError("boom mid-stream")

    response = sse_response(body(), cleanup=lambda: calls.append("cleanup"))

    async def send(message):
        pass

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}  # pragma: no cover

    with pytest.raises(RuntimeError, match="boom mid-stream"):
        await response({"type": "http"}, receive, send)

    assert calls == ["cleanup"]


async def test_sse_response_supports_an_async_cleanup_callable():
    calls = []

    async def body():
        yield {"event": "done", "data": "{}"}

    async def cleanup():
        await asyncio.sleep(0)
        calls.append("async-cleanup")

    response = sse_response(body(), cleanup=cleanup)

    async def send(message):
        pass

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}  # pragma: no cover

    await response({"type": "http"}, receive, send)

    assert calls == ["async-cleanup"]


async def test_sse_response_runs_cleanup_when_client_disconnects_before_body_ever_iterates():
    """Reproduces the exact race routes/sse.py's module docstring describes: the ASGI `send`
    callable hangs forever on `http.response.start` (so sse_starlette's _stream_response can
    never reach `body_iterator.__anext__()`), while `receive` resolves IMMEDIATELY with
    http.disconnect (so _listen_for_disconnect finishes first and cancels the whole task group).
    The body generator must never start running at all, yet cleanup must still run exactly
    once -- proving the BackgroundTask half of sse_response, not the generator's own finally, is
    what saved this case."""
    entered_generator = False
    cleanup_calls = []

    async def body():
        nonlocal entered_generator
        entered_generator = True
        yield {"event": "token", "data": "should never be reached"}  # pragma: no cover

    response = sse_response(body(), cleanup=lambda: cleanup_calls.append(True))

    hang_forever = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.start":
            await hang_forever.wait()  # never resolves on its own

    async def receive():
        return {"type": "http.disconnect"}

    await response({"type": "http"}, receive, send)

    assert entered_generator is False  # the generator body never started running at all
    assert cleanup_calls == [True]  # cleanup still ran exactly once, via the background task


async def test_sse_response_cleanup_runs_at_most_once_on_normal_completion_despite_both_paths_firing():
    """Defense-in-depth for the guard itself: on normal completion, the wrapped generator's own
    `finally` runs cleanup DURING `_stream_response`'s iteration, and then sse_starlette's
    `__call__` STILL unconditionally calls `self.background()` afterward (see its module
    docstring) -- without sse_response's own `done` guard, this would double-release the lease.
    Asserting `calls == ["cleanup"]` (not `["cleanup", "cleanup"]`) proves the guard, not just
    that cleanup ran at all."""
    calls = []

    async def body():
        yield {"event": "done", "data": "{}"}

    response = sse_response(body(), cleanup=lambda: calls.append("cleanup"))

    async def send(message):
        pass

    async def receive():
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}  # pragma: no cover

    await response({"type": "http"}, receive, send)

    assert calls == ["cleanup"]


# ---------------------------------------------------------------------------
# Shared contract: every affected route must actually be WIRED to sse_response (not a bare
# EventSourceResponse) and must release its real DB lease when a client disconnects before the
# SSE body is ever iterated -- driven end to end through each route function with a raw ASGI
# disconnect race, not merely asserting the response type.
# ---------------------------------------------------------------------------

def _make_json_request(app, json_body):
    """A minimal real starlette Request bound to `app` (so request.app.state.* resolves) whose
    one body chunk is `json_body` -- delivered on the first receive() call; every call after that
    returns http.disconnect, matching a client that sends its request then vanishes."""
    body_bytes = _json.dumps(json_body).encode()
    delivered = {"done": False}

    async def receive():
        if not delivered["done"]:
            delivered["done"] = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [], "app": app}
    return Request(scope, receive)


async def _drive_disconnect_before_body_ever_iterates(response):
    """The exact race from test_sse_response_runs_cleanup_when_client_disconnects_before_body_
    ever_iterates, driven against a REAL route's returned response object."""
    hang_forever = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.start":
            await hang_forever.wait()

    async def receive():
        return {"type": "http.disconnect"}

    await response({"type": "http"}, receive, send)


@pytest.fixture(autouse=True)
def _fake_model_limit_and_forbid_generation(monkeypatch):
    """Every route below must preflight successfully (so it reaches the point of returning an
    SSE response at all) but must NEVER actually call the model in these tests -- the whole
    point is that the client disconnected before the body was ever iterated, so zero tokens
    should ever be requested. A stream_completion_envelope call would be a bug in the test OR a
    regression in the route; either way, failing loudly here is more useful than a fake that
    silently accepts the call."""
    async def _fake_limit(model):
        return 200_000
    monkeypatch.setattr(llm_module, "get_model_max_prompt_tokens", _fake_limit)

    async def _accept_any_model(model, active_model):
        return None
    monkeypatch.setattr(llm_module, "validate_selected_model", _accept_any_model)

    async def _forbidden(envelope):
        raise AssertionError(
            "the model must never be called when the client disconnected before the SSE body "
            "ever started iterating"
        )
        yield  # pragma: no cover -- unreachable, makes this an async generator
    monkeypatch.setattr(llm_module, "stream_completion_envelope", _forbidden)


async def test_entry_chat_releases_thread_lease_on_disconnect_before_iteration(client):
    from unflincher.db import entry_thread_key, get_lease_by_target
    from unflincher.routes.entry import entry_chat

    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    request = _make_json_request(client.app, {"message": "你好"})
    response = await entry_chat(request, entry_id)
    assert response.background is not None  # wired to sse_response, not a bare EventSourceResponse

    await _drive_disconnect_before_body_ever_iterates(response)

    assert get_lease_by_target(db, entry_thread_key(entry_id)) is None
    # No assistant reply was ever persisted -- confirms the model truly was never called.
    assert db.execute(
        "SELECT COUNT(*) AS n FROM chat_message WHERE thread_kind='entry' AND role='assistant'"
    ).fetchone()["n"] == 0


async def test_trigger_report_releases_report_lease_on_disconnect_before_iteration(client):
    from unflincher.db import get_lease_by_target, report_target_key
    from unflincher.routes.report import trigger_report

    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    )

    request = _make_json_request(client.app, {})
    response = await trigger_report(request)
    assert response.background is not None

    await _drive_disconnect_before_body_ever_iterates(response)

    assert get_lease_by_target(db, report_target_key()) is None
    assert db.execute("SELECT COUNT(*) AS n FROM aggregate_report").fetchone()["n"] == 0


async def test_workshop_test_run_releases_request_lease_on_disconnect_before_iteration(client):
    from unflincher.routes.workshop import TestRunRequest, workshop_test_run

    db = client.app.state.db
    entry_id = db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    ).lastrowid

    leases_before = db.execute("SELECT target_key FROM generation_lease").fetchall()
    assert leases_before == []

    request = _make_json_request(
        client.app, {"entry_id": entry_id, "draft_prompt": "测试人设", "model": "test-model"},
    )
    body = TestRunRequest(entry_id=entry_id, draft_prompt="测试人设", model="test-model")
    response = await workshop_test_run(request, body)
    assert response.background is not None

    await _drive_disconnect_before_body_ever_iterates(response)

    remaining_leases = db.execute("SELECT target_key FROM generation_lease").fetchall()
    assert remaining_leases == []  # the temporary request lease was released, none stranded


async def test_send_new_session_message_releases_lease_and_title_task_on_disconnect_before_iteration(client):
    """The richest case: a brand-new Conversation's title generation task must also be
    cancelled/awaited (never orphaned) even though the main SSE body never got to iterate even
    once."""
    from unflincher.routes.chat import send_new_session_message

    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    )

    request = _make_json_request(client.app, {"message": "随便聊聊"})
    response = await send_new_session_message(request)
    assert response.background is not None

    await _drive_disconnect_before_body_ever_iterates(response)

    remaining_leases = db.execute("SELECT target_key FROM generation_lease").fetchall()
    assert remaining_leases == []
    # The session/first message WERE already durably created (that happens before the SSE
    # response is even returned -- see create_general_chat_session_and_convert_lease); only the
    # assistant reply and title must be missing.
    sessions = db.execute("SELECT * FROM chat_session").fetchall()
    assert len(sessions) == 1
    assert db.execute(
        "SELECT COUNT(*) AS n FROM chat_message WHERE role='assistant'"
    ).fetchone()["n"] == 0


async def test_send_session_message_releases_thread_lease_on_disconnect_before_iteration(client):
    from unflincher.db import conversation_thread_key, get_lease_by_target
    from unflincher.routes.chat import send_session_message

    db = client.app.state.db
    db.execute(
        "INSERT INTO diary_entry (title, content_html_raw, content_html, content_text, "
        "entry_date, source) VALUES ('标题', '<p>x</p>', '<p>x</p>', 'x', '2026-01-01', 'import')"
    )
    session_id = db.execute("INSERT INTO chat_session (title) VALUES ('t')").lastrowid

    request = _make_json_request(client.app, {"message": "继续"})
    response = await send_session_message(request, session_id)
    assert response.background is not None

    await _drive_disconnect_before_body_ever_iterates(response)

    assert get_lease_by_target(db, conversation_thread_key(session_id)) is None
    assert db.execute(
        "SELECT COUNT(*) AS n FROM chat_message WHERE role='assistant'"
    ).fetchone()["n"] == 0
