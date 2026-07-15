"""LLM orchestration. Every generation path (real per-entry commentary, aggregate report,
both chat surfaces, and the prompt-workshop test-run preview) goes through the SAME
generate_* functions here with the SAME context-assembly logic — this module never writes
to the database. Persisting a generation (or not, for test-run) is entirely the caller's
responsibility (see Task 9's route and Task 13's worker vs. Task 12's test-run route)."""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from copilot import CopilotClient
from unflincher.config import load_settings
from unflincher.context_budget import BudgetCheck, ModelLimitsUnavailableError, preflight_envelope
from unflincher.request_envelope import RequestEnvelope, build_envelope

logger = logging.getLogger(__name__)

# Shared, lifecycle-managed CopilotClient — see docs/superpowers/specs/2026-07-10-diary-persistent-
# copilot-client-design.md for the full rationale. Starting/stopping a fresh CopilotClient per LLM
# call was measured to cost ~2.5-6s of pure process-lifecycle overhead (CLI subprocess spawn +
# teardown) on top of actual model inference time — this singleton amortizes that cost across the
# app's entire uptime instead of paying it on every single call.
#
# Lifecycle state machine (race-free by construction, see _admit_generation/_release_generation/
# _begin_transition/_end_transition/_stop_shared_client below):
#   - _client / _client_generation: the current shared client instance and a monotonically
#     bumped counter identifying it, so a caller that observed one generation can never mistake a
#     LATER instance for the one it originally saw (ABA protection).
#   - _active_count: how many admitted generations (stream_completion calls, plus brief
#     warm-up/model-list reads) currently hold a reference to _client. A refresh or
#     transport-triggered replacement must wait for this to reach zero before tearing the client
#     down, so it can never yank the client out from under a DIFFERENT still-streaming caller.
#   - _refresh_active: true while a refresh or replacement is transitioning the client. New
#     admissions wait for this to clear before incrementing _active_count, so the active count can
#     actually reach (and stay at) zero for the transition to proceed — this is what prevents the
#     failing/refreshing caller from deadlocking against a stream of never-ending new admissions.
#   - _lifecycle_cond: the asyncio.Condition guarding all of the above. Held only for the brief
#     bookkeeping sections below, never for an entire stream — long operations (client.start(),
#     client.stop(), the actual token stream) happen either inside the lock only when they must
#     (client.start(), to serialize concurrent cold starts) or outside it entirely (client.stop(),
#     the stream loop itself).
_client: CopilotClient | None = None
_client_generation: int = 0
_active_count: int = 0
_refresh_active: bool = False
_lifecycle_cond = asyncio.Condition()

# Bounds how many sessions can be actively streaming on the shared client at once. Requests
# beyond this limit simply await the semaphore (no rejection, no "busy" UI) — this protects the
# single CLI subprocess from being overwhelmed if several browser tabs are chatting at once plus
# a batch regeneration job plus a background title-generation task are all in flight together.
_llm_semaphore = asyncio.Semaphore(load_settings().llm_concurrency)


async def _start_new_client() -> CopilotClient:
    client = CopilotClient()
    try:
        await client.start()
    except Exception:
        # A client that fails to start may still have partially launched the CLI subprocess;
        # force_stop() is the best-effort cleanup for that half-started state so we never leak an
        # orphan process on a failed startup attempt.
        with contextlib.suppress(Exception):
            await client.force_stop()
        raise
    return client


async def _ensure_client() -> tuple[CopilotClient, int]:
    """Return the shared client (starting a new one if none exists yet) and the generation number
    it was created under, WITHOUT counting as an admitted generation (no _active_count bump) and
    without waiting on any in-progress refresh — used only by warm_up_client() at boot (before
    any refresh/replacement can possibly be in progress) and internally by the transition helpers
    below. Real generation admission goes through _admit_generation() instead."""
    global _client, _client_generation
    async with _lifecycle_cond:
        if _client is None:
            _client = await _start_new_client()
            _client_generation += 1
        return _client, _client_generation


async def _admit_generation() -> tuple[CopilotClient, int]:
    """Wait while a refresh/replacement is in progress, then increment the active-session count
    under the lifecycle lock before returning the shared client (starting one if needed). This is
    the ONLY way a real generation (stream_completion) or a model-list validation read observes
    or uses the shared client — see the module-level state-machine docstring above."""
    global _client, _client_generation, _active_count
    async with _lifecycle_cond:
        await _lifecycle_cond.wait_for(lambda: not _refresh_active)
        if _client is None:
            _client = await _start_new_client()
            _client_generation += 1
        _active_count += 1
        return _client, _client_generation


async def _release_generation() -> None:
    """Decrement the active-session count and wake anyone waiting for it to reach zero (a
    pending refresh or transport-triggered replacement). Always called in a `finally`."""
    global _active_count
    async with _lifecycle_cond:
        _active_count -= 1
        _lifecycle_cond.notify_all()


async def _begin_transition() -> None:
    """Claim the exclusive 'transitioning the client' role: wait for any other transition to
    finish, mark one active (which blocks all NEW admissions from this point via
    _admit_generation's wait_for), then wait for every already-admitted generation to finish.
    Must always be paired with _end_transition(), even on failure."""
    global _refresh_active
    async with _lifecycle_cond:
        await _lifecycle_cond.wait_for(lambda: not _refresh_active)
        _refresh_active = True
        await _lifecycle_cond.wait_for(lambda: _active_count == 0)


async def _end_transition(new_client: CopilotClient | None) -> None:
    """Install new_client (or None) as the shared client, bump the generation if a real client
    was installed, clear the transition flag, and release every admission waiting on it."""
    global _client, _client_generation, _refresh_active
    async with _lifecycle_cond:
        _client = new_client
        if new_client is not None:
            _client_generation += 1
        _refresh_active = False
        _lifecycle_cond.notify_all()


async def _stop_shared_client(expected_generation: int) -> None:
    """Replace the shared client, but ONLY if it's still the instance the caller last observed
    (expected_generation matches the current generation) — see the ABA-race comment in the
    module-level docstring. Blocks new admissions and waits for every OTHER already-admitted
    generation to finish before tearing the client down, so this can never terminate a
    concurrently streaming peer. The caller (stream_completion's transport-failure path) must
    release its OWN admission before calling this, or it would deadlock waiting on itself.

    A no-op if another caller already replaced the client (ABA-safe): at most one caller per
    stale generation actually stops anything."""
    global _client, _refresh_active
    async with _lifecycle_cond:
        if _client is None or _client_generation != expected_generation:
            return
        await _lifecycle_cond.wait_for(lambda: not _refresh_active)
        if _client is None or _client_generation != expected_generation:
            return
        _refresh_active = True
        await _lifecycle_cond.wait_for(lambda: _active_count == 0)
        client_to_stop = _client
        _client = None
    with contextlib.suppress(Exception):
        await client_to_stop.stop()
    async with _lifecycle_cond:
        _refresh_active = False
        _lifecycle_cond.notify_all()


async def warm_up_client() -> None:
    """Called from app.py's lifespan at startup. Best-effort: a transient auth hiccup at boot
    must never prevent the app from starting — the first real request will retry via the same
    _ensure_client() path this function itself uses."""
    try:
        await _ensure_client()
    except Exception:
        logger.warning("warm_up_client: failed to start the shared Copilot client at boot", exc_info=True)


async def shutdown_client() -> None:
    """Called from app.py's lifespan at shutdown. Routed through the same transition state
    machine as refresh/transport-replacement (waits for any in-flight generation to finish before
    tearing the client down) so a clean shutdown can never kill an in-progress stream out from
    under a caller — unlike _stop_shared_client, there is no caller-observed generation to check
    against, since a clean shutdown always wins regardless of which generation is current."""
    await _begin_transition()
    client_to_stop = _client
    try:
        if client_to_stop is not None:
            with contextlib.suppress(Exception):
                await client_to_stop.stop()
    finally:
        await _end_transition(None)



PER_ENTRY_TASK = "重点回应这一篇日记，但可以参考其他日记里反复出现的模式，引用具体时间点。"
REPORT_TASK = "读完全部日记后，写一份看跨越时间的模式、反复出现的困惑、成长轨迹的综合报告。"
CHAT_TASK = "这是延续对话，保持上下文一致，语气跟你平时逐篇/总对话一致。"

# Stall / no-progress timeout, NOT a total-duration cap: it bounds only the wait for the NEXT
# event and resets whenever any event arrives, so a slow-but-actively-streaming completion is
# never killed. It fires only when the CLI subprocess wedges or crashes (e.g. OOM kill, segfault)
# and closes its pipe WITHOUT emitting an idle/error event (the SDK's on_close just marks the
# client disconnected, it does not synthesize a SessionErrorData). Kept generous because this path
# is a single-turn, tool-free, non-web-search text completion over the full ~55-62K-token diary
# corpus, where a slow model can legitimately take a while before the first/next token.
_STALL_TIMEOUT_SECONDS = 120.0


class ModelSessionError(RuntimeError):
    """The model/session itself reported an error (bad model name, revoked auth, rate limit,
    etc. — anything the SDK surfaces via a SessionErrorData event). Never retried: restarting the
    CLI subprocess does not fix any of these, and retrying would silently mask a real, actionable
    error behind an extra 1-3s delay and a second, more confusing failure."""


class TransportStalledError(RuntimeError):
    """No event arrived for _STALL_TIMEOUT_SECONDS — the CLI subprocess likely crashed or wedged
    without reporting an error. Classified as a transport failure (retry-eligible if no token has
    been yielded yet), same bucket as ProcessExitedError/ConnectionError/OSError."""


async def stream_completion_envelope(envelope: RequestEnvelope) -> AsyncIterator[str]:
    """Stream one completion token-by-token via the GitHub Copilot SDK, from the EXACT prepared
    RequestEnvelope a caller already built (and, for any real generation path, already preflighted
    — see context_budget.preflight_envelope). This is the ONLY place the whole app talks to an
    LLM; every other entry point in this module (stream_completion, generate_from_prepared, and
    every generate_*/chat_* convenience wrapper below) funnels through here, and every SDK kwarg
    below is derived FROM the envelope's own fields — never reconstructed from separate strings —
    so a caller can never observe a smaller proxy at preflight time than what the model actually
    receives.

    Authenticates with COPILOT_GITHUB_TOKEN (a shared fine-grained GitHub PAT injected by the
    Quadlet unit) — CopilotClient() auto-detects that env var, so no key is passed in code.

    Uses the shared, lifecycle-managed client (_admit_generation()) instead of starting/stopping a
    fresh CopilotClient per call — see docs/superpowers/specs/2026-07-10-diary-persistent-copilot-
    client-design.md and the module-level state-machine docstring above. A transport-level failure
    (ProcessExitedError, ConnectionError, OSError, or TransportStalledError) that happens BEFORE
    any token has been yielded triggers exactly one reset-and-retry; the same failure after a
    token has already reached the caller propagates immediately (retrying then would duplicate or
    splice together two different model attempts). A ModelSessionError (the model/session itself
    reported an error) is never retried, regardless of whether tokens were yielded — it isn't a
    transport problem restarting the subprocess would fix. Any OTHER exception (including every
    domain/safety error in this app -- ContextTooLargeError, ModelLimitsUnavailableError,
    MaintenanceLockedError, TargetBusyError, ArchiveChangedError, RequestFormatChangedError,
    StaleOrSupersededRetryError, all of which are RuntimeError subclasses) always propagates
    immediately and is NEVER treated as a retryable transport failure -- the retry tuple below is
    deliberately narrow, not a bare RuntimeError catch.

    On a retryable transport failure, this releases its OWN admission BEFORE asking the lifecycle
    state machine to replace the client generation it observed, then re-admits for its single
    retry — see _stop_shared_client's docstring for why the release must happen first (otherwise
    the failing caller would deadlock waiting on its own still-held admission)."""
    from copilot.client import ProcessExitedError
    from copilot.generated.session_events import (
        AssistantIdleData,
        AssistantMessageDeltaData,
        SessionErrorData,
    )

    retryable_transport_errors = (ProcessExitedError, TransportStalledError, ConnectionError, OSError)

    for attempt in range(2):  # at most one retry, per the docstring above
        async with _llm_semaphore:
            client, generation = await _admit_generation()
            admitted = True
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()

            def _on_event(event):
                loop.call_soon_threadsafe(queue.put_nowait, event)

            unsubscribe = None
            session_id: str | None = None
            yielded_any = False
            try:
                session = await client.create_session(
                    model=envelope.model,
                    # "replace" mode: use diary's own persona+task text verbatim as the system
                    # prompt. The SDK's default is its full general-purpose coding-agent system
                    # prompt (identity/tone/tool/environment/code-change sections); that would
                    # contaminate the life-mentor persona. Do NOT simplify this back to the SDK
                    # default.
                    system_message={"mode": envelope.system_mode, "content": envelope.system_content},
                    # Pure text-in/text-out. An empty allowlist disables the entire merged tool
                    # catalog (built-in + MCP + custom) so the model can never touch this server's
                    # filesystem or network. Security requirement, not an optimization — keep it
                    # empty.
                    available_tools=list(envelope.available_tools),
                    # Isolate from any ambient AGENTS.md/.github/copilot-instructions on the host:
                    # custom instruction files load based on working_directory regardless of config
                    # discovery, so point it at /tmp (guaranteed to exist, guaranteed to hold no
                    # instruction files) and belt-and-suspenders the rest off.
                    working_directory=envelope.working_directory,
                    skip_custom_instructions=envelope.skip_custom_instructions,
                    enable_config_discovery=envelope.enable_config_discovery,
                    enable_skills=envelope.enable_skills,
                    streaming=envelope.streaming,
                    on_event=_on_event,
                )
                session_id = session.session_id
                # Register once more to capture the unsubscribe callable for guaranteed cleanup. on()
                # adds to a set, so re-registering the same handler is idempotent (it still fires
                # once/event).
                unsubscribe = session.on(_on_event)
                await session.send(envelope.user_content)
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=_STALL_TIMEOUT_SECONDS)
                    except (asyncio.TimeoutError, TimeoutError) as exc:
                        raise TransportStalledError(
                            f"Copilot SDK stream stalled: no event received for "
                            f"{_STALL_TIMEOUT_SECONDS}s (CLI subprocess likely crashed or wedged "
                            "without reporting an error)"
                        ) from exc
                    data = event.data
                    if isinstance(data, AssistantMessageDeltaData):
                        if data.delta_content:
                            yielded_any = True
                            yield data.delta_content
                    elif isinstance(data, SessionErrorData):
                        raise ModelSessionError(f"Copilot SDK session error: {data.message}")
                    elif isinstance(data, AssistantIdleData):
                        break
                    # Ignore everything else (reasoning/lifecycle/etc.) and keep reading.
                return  # success — do not fall through to the retry loop
            except ModelSessionError:
                raise  # never retried, regardless of yielded_any
            except retryable_transport_errors:
                if yielded_any or attempt == 1:
                    # Either we already sent partial output downstream (retrying would corrupt it),
                    # or this WAS the retry attempt and it failed too — propagate either way.
                    raise
                # First attempt failed before any token reached the caller: release OUR OWN
                # admission FIRST (so the replacement below can observe _active_count reaching
                # zero without waiting on itself), then ask the lifecycle state machine to replace
                # the client generation we observed (only if it's still current — ABA-safe), and
                # let the `for` loop's next iteration re-admit and retry once.
                await _release_generation()
                admitted = False
                await _stop_shared_client(generation)
                continue
            finally:
                if admitted:
                    await _release_generation()
                if unsubscribe is not None:
                    unsubscribe()
                if session_id is not None:
                    with contextlib.suppress(Exception):
                        await client.delete_session(session_id)


async def stream_completion(
    system: str,
    user_content: str,
    model: str,
    *,
    target_kind: str = "direct",
    target_id: str | int | None = None,
) -> AsyncIterator[str]:
    """Convenience wrapper for callers that have not (or need not) preflight a request: builds
    ONE RequestEnvelope from the given strings and streams it via stream_completion_envelope().
    Any caller that DOES preflight a request (see prepare_commentary_request and friends below)
    must instead call stream_completion_envelope() directly with the SAME envelope object that was
    preflighted — never rebuild one from strings after preflight, or the preflight check could
    measure a proxy while the model receives something that was never actually validated."""
    envelope = build_envelope(system, user_content, model, target_kind=target_kind, target_id=target_id)
    async for token in stream_completion_envelope(envelope):
        yield token


async def generate_from_prepared(prepared: "PreparedRequest") -> AsyncIterator[str]:
    """Stream a completion from an already-prepared-and-preflighted request (see prepare_*_request
    functions below). Passes the EXACT SAME envelope object through to the SDK call — the whole
    point of the shared prepared-request interface."""
    async for token in stream_completion_envelope(prepared.envelope):
        yield token


def _build_corpus(all_entries: list[dict]) -> str:
    parts = []
    for i, e in enumerate(all_entries, start=1):
        parts.append(f"#{i} [{e['entry_date'][:10]}] {e['title']}\n{e['content_text']}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Request assembly (pure, no I/O) — the ONE place system/user content is built for each
# generation path. Every generate_*/chat_* convenience wrapper AND every prepare_*_request
# preflight-integrated function below is built on these same functions, so preflight and real
# generation can never see different content for the same logical call.
# ---------------------------------------------------------------------------

def commentary_content(entry: dict, all_entries: list[dict], persona_text: str) -> tuple[str, str]:
    system = f"{persona_text}\n\n{PER_ENTRY_TASK}"
    corpus = _build_corpus(all_entries)
    target_index = next(i for i, e in enumerate(all_entries, start=1) if e["id"] == entry["id"])
    user_content = (
        f"全部日记（供跨篇参考）：\n\n{corpus}\n\n---\n\n"
        f"现在请针对第 {target_index} 篇写锐评：{entry['title']}"
    )
    return system, user_content


def report_content(all_entries: list[dict], persona_text: str) -> tuple[str, str]:
    system = f"{persona_text}\n\n{REPORT_TASK}"
    user_content = f"全部日记（按时间顺序）：\n\n{_build_corpus(all_entries)}"
    return system, user_content


def entry_chat_content(
    entry_context: dict, commentary_text: str | None, history: list[dict],
    user_message: str, persona_text: str,
) -> tuple[str, str]:
    system_parts = [persona_text, CHAT_TASK, f"这条对话是关于这篇日记：{entry_context['title']}\n{entry_context['content_text']}"]
    if commentary_text:
        system_parts.append(f"你目前对这篇日记最新的锐评是：{commentary_text}")
    system = "\n\n".join(system_parts)
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    user_content = f"{history_text}\nuser: {user_message}" if history else user_message
    return system, user_content


def general_chat_content(
    all_entries: list[dict], history: list[dict], user_message: str, persona_text: str,
) -> tuple[str, str]:
    system = f"{persona_text}\n\n{CHAT_TASK}\n\n全部日记（供参考）：\n\n{_build_corpus(all_entries)}"
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    user_content = f"{history_text}\nuser: {user_message}" if history else user_message
    return system, user_content


_TITLE_SYSTEM_PROMPT = (
    "用不超过10个汉字概括这条消息想聊的主题，只输出主题本身，不要标点、不要解释、不要引号。"
)


def title_content(first_message: str) -> tuple[str, str]:
    return _TITLE_SYSTEM_PROMPT, first_message


# ---------------------------------------------------------------------------
# Envelope builders (pure, no I/O) — one RequestEnvelope per generation path, built from the
# content-assembly functions above. Used directly by callers that need to preflight a BATCH of
# requests against one already-fetched model limit (e.g. apply-all) without paying for a fresh
# model-list fetch per item; prepare_*_request below is the one-shot, single-call convenience path.
# ---------------------------------------------------------------------------

def build_commentary_envelope(entry: dict, all_entries: list[dict], persona_text: str, model: str) -> RequestEnvelope:
    system, user_content = commentary_content(entry, all_entries, persona_text)
    return build_envelope(system, user_content, model, target_kind="entry_commentary", target_id=entry["id"])


def build_report_envelope(all_entries: list[dict], persona_text: str, model: str) -> RequestEnvelope:
    system, user_content = report_content(all_entries, persona_text)
    return build_envelope(system, user_content, model, target_kind="aggregate_report")


def build_entry_chat_envelope(
    entry_context: dict, commentary_text: str | None, history: list[dict],
    user_message: str, persona_text: str, model: str,
) -> RequestEnvelope:
    system, user_content = entry_chat_content(entry_context, commentary_text, history, user_message, persona_text)
    return build_envelope(system, user_content, model, target_kind="entry_chat", target_id=entry_context["id"])


def build_general_chat_envelope(
    all_entries: list[dict], history: list[dict], user_message: str, persona_text: str, model: str,
) -> RequestEnvelope:
    system, user_content = general_chat_content(all_entries, history, user_message, persona_text)
    return build_envelope(system, user_content, model, target_kind="general_chat")


def build_title_envelope(first_message: str, model: str) -> RequestEnvelope:
    system, user_content = title_content(first_message)
    return build_envelope(system, user_content, model, target_kind="conversation_title")


@dataclass(frozen=True)
class PreparedRequest:
    """One already-preflighted request: the exact envelope that was validated, plus the budget
    check that validated it. generate_from_prepared() streams from prepared.envelope unchanged."""

    envelope: RequestEnvelope
    budget: BudgetCheck


async def _prepare(envelope: RequestEnvelope) -> PreparedRequest:
    """Fetch the model's CURRENT published max_prompt_tokens and preflight the given envelope
    against it. Raises ModelLimitsUnavailableError or ContextTooLargeError -- callers must do this
    BEFORE opening any SSE stream or making any durable domain write."""
    limit = await get_model_max_prompt_tokens(envelope.model)
    budget = preflight_envelope(envelope, limit)
    return PreparedRequest(envelope=envelope, budget=budget)


async def prepare_commentary_request(entry: dict, all_entries: list[dict], persona_text: str, model: str) -> PreparedRequest:
    return await _prepare(build_commentary_envelope(entry, all_entries, persona_text, model))


async def prepare_report_request(all_entries: list[dict], persona_text: str, model: str) -> PreparedRequest:
    return await _prepare(build_report_envelope(all_entries, persona_text, model))


async def prepare_entry_chat_request(
    entry_context: dict, commentary_text: str | None, history: list[dict],
    user_message: str, persona_text: str, model: str,
) -> PreparedRequest:
    return await _prepare(
        build_entry_chat_envelope(entry_context, commentary_text, history, user_message, persona_text, model)
    )


async def prepare_general_chat_request(
    all_entries: list[dict], history: list[dict], user_message: str, persona_text: str, model: str,
) -> PreparedRequest:
    return await _prepare(build_general_chat_envelope(all_entries, history, user_message, persona_text, model))


async def prepare_title_request(first_message: str, model: str) -> PreparedRequest:
    return await _prepare(build_title_envelope(first_message, model))


# ---------------------------------------------------------------------------
# Convenience generation wrappers (no preflight) — kept for callers that intentionally skip
# preflight (none of this app's real user-facing paths should; see routes/worker for the
# preflight-integrated prepare_*_request + generate_from_prepared path each of them uses).
# ---------------------------------------------------------------------------

async def generate_commentary(
    entry: dict, all_entries: list[dict], persona_text: str, model: str
) -> AsyncIterator[str]:
    system, user_content = commentary_content(entry, all_entries, persona_text)
    async for token in stream_completion(system, user_content, model, target_kind="entry_commentary", target_id=entry["id"]):
        yield token


async def generate_report(all_entries: list[dict], persona_text: str, model: str) -> AsyncIterator[str]:
    system, user_content = report_content(all_entries, persona_text)
    async for token in stream_completion(system, user_content, model, target_kind="aggregate_report"):
        yield token


async def chat_reply(
    entry_context: dict, commentary_text: str | None, history: list[dict],
    user_message: str, persona_text: str, model: str,
) -> AsyncIterator[str]:
    system, user_content = entry_chat_content(entry_context, commentary_text, history, user_message, persona_text)
    async for token in stream_completion(system, user_content, model, target_kind="entry_chat", target_id=entry_context["id"]):
        yield token


async def general_chat_reply(
    all_entries: list[dict], history: list[dict], user_message: str, persona_text: str, model: str,
) -> AsyncIterator[str]:
    system, user_content = general_chat_content(all_entries, history, user_message, persona_text)
    async for token in stream_completion(system, user_content, model, target_kind="general_chat"):
        yield token


async def generate_session_title(first_message: str, model: str) -> str:
    """One-shot title generation for a newly (lazily) created chat session, used by
    routes/chat.py. Reuses stream_completion — the same seam every other generation path goes
    through — rather than a separate LLM entry point; the caller just joins the chunks since a
    short title has no benefit from token-by-token streaming to the browser."""
    system, user_content = title_content(first_message)
    chunks = [
        t async for t in stream_completion(system, user_content, model, target_kind="conversation_title")
    ]
    return "".join(chunks).strip()


async def list_available_models() -> list[tuple[str, str]]:
    """(id, display_name) pairs for the workshop dropdown, excluding the "auto" meta-model (it
    hides which model actually produced a given commentary, so it's never offered as a choice).
    Goes through the same admission rules as a real generation (waits while a refresh/
    replacement is in progress, then briefly counts toward the active-session total) — see the
    module-level state-machine docstring above: "model-list reads used for validation go through
    the same lifecycle rules"."""
    client, _ = await _admit_generation()
    try:
        models = await client.list_models()
    finally:
        await _release_generation()
    return [(m.id, m.name) for m in models if m.id != "auto"]


async def _list_model_infos() -> list:
    """Raw SDK ModelInfo objects (id, name, capabilities, ...), excluding "auto". Kept separate
    from list_available_models() so that function's (id, name) tuple return type never has to
    change; used by get_model_max_prompt_tokens() below for context-budget preflight."""
    client, _ = await _admit_generation()
    try:
        models = await client.list_models()
    finally:
        await _release_generation()
    return [m for m in models if m.id != "auto"]


async def get_model_max_prompt_tokens(model: str) -> int:
    """The selected model's published capabilities.limits.max_prompt_tokens, used for
    context-budget preflight (see context_budget.preflight_envelope). Raises
    ModelLimitsUnavailableError -- maps to a stable, retryable 503 -- if the model list cannot be
    fetched, the model is not present in the current catalog, or it advertises no prompt-token
    limit. Never guesses a limit and never lets a caller silently continue without one."""
    try:
        models = await _list_model_infos()
    except ModelLimitsUnavailableError:
        raise
    except Exception as exc:
        raise ModelLimitsUnavailableError(model, f"model list unavailable: {exc}") from exc
    for info in models:
        if info.id == model:
            limits = getattr(info.capabilities, "limits", None)
            limit = getattr(limits, "max_prompt_tokens", None)
            if limit is None:
                raise ModelLimitsUnavailableError(model, "model advertises no max_prompt_tokens")
            return limit
    raise ModelLimitsUnavailableError(model, "model not found in current catalog")


class UnsupportedModelError(RuntimeError):
    """The requested model id is not present in the current model catalog. Maps to a stable,
    NON-retryable 400 unsupported_model -- unlike ModelLimitsUnavailableError (503), this means
    the catalog loaded fine and simply does not contain this id, so retrying with the same id
    would never succeed; the caller must pick a different model."""

    def __init__(self, model: str):
        self.model = model
        super().__init__(f"model {model!r} is not present in the current model catalog")


async def validate_selected_model(model: str, active_model: str) -> None:
    """Validate a client-selected model before the Prompt Workshop generates or persists
    anything. The CURRENTLY ACTIVE model is always accepted without a catalog round trip, even
    when the catalog is temporarily unreachable -- continuing to use the model already in
    production use must never be blocked by a transient model-list outage. Any OTHER (changed)
    model must exist in the latest catalog: raises UnsupportedModelError (stable 400) if the
    catalog loads but doesn't contain it, or ModelLimitsUnavailableError (retryable 503) if the
    catalog itself could not be fetched."""
    if model == active_model:
        return
    try:
        models = await _list_model_infos()
    except Exception as exc:
        raise ModelLimitsUnavailableError(model, f"model catalog unavailable: {exc}") from exc
    if not any(info.id == model for info in models):
        raise UnsupportedModelError(model)


async def refresh_available_models() -> list[tuple[str, str]]:
    """Force a fresh fetch, busting the SDK's in-process model-list cache. The SDK only clears
    that cache when the client disconnects (no public API invalidates just the cache), so this is
    implemented as a full client restart, routed through the same transition state machine as a
    transport-triggered replacement (_begin_transition/_end_transition): new admissions are
    blocked from the moment this starts, so it WAITS for any currently-active generation to finish
    rather than refusing outright (the old instant-reject behaviour) — the active count is then
    guaranteed to actually reach zero instead of racing against ever-arriving new admissions."""
    await _begin_transition()
    old_client = _client
    try:
        if old_client is not None:
            with contextlib.suppress(Exception):
                await old_client.stop()
        new_client = await _start_new_client()
    except Exception:
        await _end_transition(None)
        raise
    await _end_transition(new_client)
    return await list_available_models()
