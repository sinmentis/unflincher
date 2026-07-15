"""One shared response-level cleanup seam, reused by every SSE route in this app (Entry
Reflection chat, direct Life Report, existing/new general Conversation, Prompt Workshop
preview).

The problem this closes: an `async def event_stream(): ... finally: release(...)` generator's
own `finally` only runs once the generator body has actually STARTED iterating. But a client can
disconnect after the route handler returns the response object and BEFORE Starlette/sse_starlette
ever calls `__anext__()` on that body for the first time -- EventSourceResponse.__call__ races a
_stream_response task against a _listen_for_disconnect task in one task group and returns as soon
as either finishes (see sse_starlette.sse.EventSourceResponse.__call__). If disconnect wins that
race before the body iterator is ever touched, the generator's own `finally` NEVER runs, and
whatever it was going to release (always a generation lease here; for a new Conversation, also an
in-flight title task) is stranded until the process restarts.

sse_response() closes that gap: it wraps the body iterator in its own try/finally (so cleanup
still runs as early as possible on ordinary completion, failure, or a disconnect mid-stream that
DOES reach the body), and ALSO attaches the exact same cleanup as a Starlette BackgroundTask --
which EventSourceResponse.__call__ runs unconditionally after its task group finishes, covering
the one case the generator's own finally cannot: disconnect before the body was ever iterated.
`cleanup` itself is a plain callable (sync or async) and is guaranteed to run at MOST once no
matter which of the two paths (generator finally, background task) fires it first."""
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable

from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask


def sse_response(
    body: AsyncIterator[dict],
    *,
    cleanup: Callable[[], "Awaitable[None] | None"],
) -> EventSourceResponse:
    """Wrap one SSE body iterator so `cleanup` runs EXACTLY ONCE regardless of how the response
    ends: iteration never starting (client disconnected immediately), disconnecting partway
    through, completing normally, or the body raising. `cleanup` may release one lease, or (for
    the new-Conversation route) cancel/await an orphaned title task before releasing the main
    lease -- callers own what "cleanup" means; this module only owns WHEN and HOW MANY TIMES it
    runs."""
    done = False

    async def _cleanup_once() -> None:
        nonlocal done
        if done:
            return
        done = True
        result = cleanup()
        if inspect.isawaitable(result):
            await result

    async def _wrapped() -> AsyncIterator[dict]:
        try:
            async for event in body:
                yield event
        finally:
            await _cleanup_once()

    return EventSourceResponse(_wrapped(), sep="\n", background=BackgroundTask(_cleanup_once))
