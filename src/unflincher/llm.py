"""LLM orchestration. Every generation path (real per-entry commentary, aggregate report,
both chat surfaces, and the prompt-workshop test-run preview) goes through the SAME
generate_* functions here with the SAME context-assembly logic — this module never writes
to the database. Persisting a generation (or not, for test-run) is entirely the caller's
responsibility (see Task 9's route and Task 13's worker vs. Task 12's test-run route)."""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from copilot import CopilotClient
from unflincher.config import load_settings

logger = logging.getLogger(__name__)

# Shared, lifecycle-managed CopilotClient — see docs/superpowers/specs/2026-07-10-diary-persistent-
# copilot-client-design.md for the full rationale. Starting/stopping a fresh CopilotClient per LLM
# call was measured to cost ~2.5-6s of pure process-lifecycle overhead (CLI subprocess spawn +
# teardown) on top of actual model inference time — this singleton amortizes that cost across the
# app's entire uptime instead of paying it on every single call.
_client: CopilotClient | None = None
# Bumped every time a NEW client instance replaces the shared singleton. _stop_shared_client()
# takes the generation the CALLER last observed and only tears down the client if it's still the
# same instance — this prevents an ABA race where request A's own retry logic tears down a NEWER
# client that request B already installed to recover from A DIFFERENT failure.
_client_generation: int = 0
_client_lock = asyncio.Lock()

# Bounds how many sessions can be actively streaming on the shared client at once. Requests
# beyond this limit simply await the semaphore (no rejection, no "busy" UI) — this protects the
# single CLI subprocess from being overwhelmed if several browser tabs are chatting at once plus
# a batch regeneration job plus a background title-generation task are all in flight together.
_llm_semaphore = asyncio.Semaphore(load_settings().llm_concurrency)

# Tracks how many stream_completion() calls are currently inside their streaming loop. Checked by
# refresh_available_models() below, which must refuse to restart the shared client (the only way
# to bust the SDK's in-process model-list cache) while a generation is actively using it — doing
# so would kill that generation's in-flight stream.
_active_session_count = 0


async def _ensure_client() -> tuple[CopilotClient, int]:
    """Return the shared client (starting a new one if none exists yet) and the generation number
    it was created under. Safe to call concurrently — only one caller actually starts a new
    client; the rest just observe the result once the lock is released."""
    global _client, _client_generation
    async with _client_lock:
        if _client is None:
            client = CopilotClient()
            try:
                await client.start()
            except Exception:
                # A client that fails to start may still have partially launched the CLI
                # subprocess; force_stop() is the best-effort cleanup for that half-started state
                # so we never leak an orphan process on a failed startup attempt.
                with contextlib.suppress(Exception):
                    await client.force_stop()
                raise
            _client = client
            _client_generation += 1
        return _client, _client_generation


async def _stop_shared_client(expected_generation: int) -> None:
    """Tear down the shared client ONLY if it's still the instance the caller last observed
    (expected_generation matches the current generation). If another caller already replaced it,
    this is a no-op — see the ABA-race comment on _client_generation above."""
    global _client, _client_generation
    async with _client_lock:
        if _client is not None and _client_generation == expected_generation:
            with contextlib.suppress(Exception):
                await _client.stop()
            _client = None


async def warm_up_client() -> None:
    """Called from app.py's lifespan at startup. Best-effort: a transient auth hiccup at boot
    must never prevent the app from starting — the first real request will retry via the same
    _ensure_client() path this function itself uses."""
    try:
        await _ensure_client()
    except Exception:
        logger.warning("warm_up_client: failed to start the shared Copilot client at boot", exc_info=True)


async def shutdown_client() -> None:
    """Called from app.py's lifespan at shutdown. Unlike _stop_shared_client(), this always tears
    down whatever the current client is (there's no caller-observed generation to race against
    during a clean app shutdown)."""
    global _client, _client_generation
    async with _client_lock:
        if _client is not None:
            with contextlib.suppress(Exception):
                await _client.stop()
            _client = None



DEFAULT_PERSONA_PROMPT = """你是用户的"人生导师"。你的任务是读用户的私人日记，帮TA看清自己的人生、反复出现的困惑和目标——而不是单纯地安慰或附和。

你的语气默认温和克制，像一个真正了解TA、心疼TA的朋友；但当你从日记里察觉到自我欺骗、逃避，或者TA在用"看似合理的理由"包装自己真正害怕的东西时，直接说出来，哪怕会让TA不舒服。你的价值不在于让TA感觉良好，而在于让TA真的看清楚。

- 不说空洞、放之四海而皆准的话，每句话都要扎根在TA写的具体内容里
- 如果看到其他日记里反复出现的模式（同样的纠结、同样的借口、同样的循环），直接指出来，并引用是哪个时间点写的
- 不套用固定结构，像朋友聊天一样自然行文即可
- 不需要每次都以提问收尾，只在你真心觉得有必要追问时才问
- 犀利是为了让TA清醒，不是为了羞辱TA"""

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


def ensure_default_persona_prompt(conn) -> None:
    """Seed the default persona on first startup — a no-op if a version already exists."""
    from unflincher.db import DEFAULT_MODEL, get_active_prompt, set_active_prompt
    if get_active_prompt(conn) is None:
        set_active_prompt(conn, DEFAULT_PERSONA_PROMPT, DEFAULT_MODEL)


class ModelSessionError(RuntimeError):
    """The model/session itself reported an error (bad model name, revoked auth, rate limit,
    etc. — anything the SDK surfaces via a SessionErrorData event). Never retried: restarting the
    CLI subprocess does not fix any of these, and retrying would silently mask a real, actionable
    error behind an extra 1-3s delay and a second, more confusing failure."""


class TransportStalledError(RuntimeError):
    """No event arrived for _STALL_TIMEOUT_SECONDS — the CLI subprocess likely crashed or wedged
    without reporting an error. Classified as a transport failure (retry-eligible if no token has
    been yielded yet), same bucket as ProcessExitedError/ConnectionError/OSError."""


async def stream_completion(system: str, user_content: str, model: str) -> AsyncIterator[str]:
    """Stream one completion token-by-token via the GitHub Copilot SDK. This is the ONLY
    place the whole app talks to an LLM; every generate_*/chat_* path funnels through here.
    Authenticates with COPILOT_GITHUB_TOKEN (a shared fine-grained GitHub PAT injected by the
    Quadlet unit) — CopilotClient() auto-detects that env var, so no key is passed in code.

    Uses the shared, lifecycle-managed client (_ensure_client()) instead of starting/stopping a
    fresh CopilotClient per call — see docs/superpowers/specs/2026-07-10-diary-persistent-copilot-
    client-design.md. A transport-level failure (ProcessExitedError, ConnectionError, OSError, or
    TransportStalledError) that happens BEFORE any token has been yielded triggers exactly one
    reset-and-retry; the same failure after a token has already reached the caller propagates
    immediately (retrying then would duplicate or splice together two different model attempts).
    A ModelSessionError (the model/session itself reported an error) is never retried, regardless
    of whether tokens were yielded — it isn't a transport problem restarting the subprocess would
    fix."""
    from copilot.client import ProcessExitedError
    from copilot.generated.session_events import (
        AssistantIdleData,
        AssistantMessageDeltaData,
        SessionErrorData,
    )

    for attempt in range(2):  # at most one retry, per the docstring above
        async with _llm_semaphore:
            global _active_session_count
            _active_session_count += 1
            client, generation = await _ensure_client()
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()

            def _on_event(event):
                loop.call_soon_threadsafe(queue.put_nowait, event)

            unsubscribe = None
            session_id: str | None = None
            yielded_any = False
            try:
                session = await client.create_session(
                    model=model,
                    # "replace" mode: use diary's own persona+task text verbatim as the system
                    # prompt. The SDK's default is its full general-purpose coding-agent system
                    # prompt (identity/tone/tool/environment/code-change sections); that would
                    # contaminate the life-mentor persona. Do NOT simplify this back to the SDK
                    # default.
                    system_message={"mode": "replace", "content": system},
                    # Pure text-in/text-out. An empty allowlist disables the entire merged tool
                    # catalog (built-in + MCP + custom) so the model can never touch this server's
                    # filesystem or network. Security requirement, not an optimization — keep it
                    # empty.
                    available_tools=[],
                    # Isolate from any ambient AGENTS.md/.github/copilot-instructions on the host:
                    # custom instruction files load based on working_directory regardless of config
                    # discovery, so point it at /tmp (guaranteed to exist, guaranteed to hold no
                    # instruction files) and belt-and-suspenders the rest off.
                    working_directory="/tmp",
                    skip_custom_instructions=True,
                    enable_config_discovery=False,
                    enable_skills=False,
                    streaming=True,
                    on_event=_on_event,
                )
                session_id = session.session_id
                # Register once more to capture the unsubscribe callable for guaranteed cleanup. on()
                # adds to a set, so re-registering the same handler is idempotent (it still fires
                # once/event).
                unsubscribe = session.on(_on_event)
                await session.send(user_content)
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
            except (ProcessExitedError, TransportStalledError, ConnectionError, OSError, RuntimeError):
                if yielded_any or attempt == 1:
                    # Either we already sent partial output downstream (retrying would corrupt it),
                    # or this WAS the retry attempt and it failed too — propagate either way.
                    raise
                # First attempt failed before any token reached the caller: reset the shared client
                # (only if it's still the same instance we observed — see _stop_shared_client's
                # ABA-race guard) and let the `for` loop's next iteration retry once.
                await _stop_shared_client(generation)
                continue
            finally:
                _active_session_count -= 1
                if unsubscribe is not None:
                    unsubscribe()
                if session_id is not None:
                    with contextlib.suppress(Exception):
                        await client.delete_session(session_id)


def _build_corpus(all_entries: list[dict]) -> str:
    parts = []
    for i, e in enumerate(all_entries, start=1):
        parts.append(f"#{i} [{e['entry_date'][:10]}] {e['title']}\n{e['content_text']}")
    return "\n\n---\n\n".join(parts)


async def generate_commentary(
    entry: dict, all_entries: list[dict], persona_text: str, model: str
) -> AsyncIterator[str]:
    system = f"{persona_text}\n\n{PER_ENTRY_TASK}"
    corpus = _build_corpus(all_entries)
    target_index = next(i for i, e in enumerate(all_entries, start=1) if e["id"] == entry["id"])
    user_content = (
        f"全部日记（供跨篇参考）：\n\n{corpus}\n\n---\n\n"
        f"现在请针对第 {target_index} 篇写锐评：{entry['title']}"
    )
    async for token in stream_completion(system, user_content, model):
        yield token


async def generate_report(all_entries: list[dict], persona_text: str, model: str) -> AsyncIterator[str]:
    system = f"{persona_text}\n\n{REPORT_TASK}"
    user_content = f"全部日记（按时间顺序）：\n\n{_build_corpus(all_entries)}"
    async for token in stream_completion(system, user_content, model):
        yield token


async def chat_reply(
    entry_context: dict, commentary_text: str | None, history: list[dict],
    user_message: str, persona_text: str, model: str,
) -> AsyncIterator[str]:
    system_parts = [persona_text, CHAT_TASK, f"这条对话是关于这篇日记：{entry_context['title']}\n{entry_context['content_text']}"]
    if commentary_text:
        system_parts.append(f"你目前对这篇日记最新的锐评是：{commentary_text}")
    system = "\n\n".join(system_parts)
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    user_content = f"{history_text}\nuser: {user_message}" if history else user_message
    async for token in stream_completion(system, user_content, model):
        yield token


async def general_chat_reply(
    all_entries: list[dict], history: list[dict], user_message: str, persona_text: str, model: str,
) -> AsyncIterator[str]:
    system = f"{persona_text}\n\n{CHAT_TASK}\n\n全部日记（供参考）：\n\n{_build_corpus(all_entries)}"
    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in history)
    user_content = f"{history_text}\nuser: {user_message}" if history else user_message
    async for token in stream_completion(system, user_content, model):
        yield token


_TITLE_SYSTEM_PROMPT = (
    "用不超过10个汉字概括这条消息想聊的主题，只输出主题本身，不要标点、不要解释、不要引号。"
)


async def generate_session_title(first_message: str, model: str) -> str:
    """One-shot title generation for a newly (lazily) created chat session, used by
    routes/chat.py. Reuses stream_completion — the same seam every other generation path goes
    through — rather than a separate LLM entry point; the caller just joins the chunks since a
    short title has no benefit from token-by-token streaming to the browser."""
    chunks = [t async for t in stream_completion(_TITLE_SYSTEM_PROMPT, first_message, model)]
    return "".join(chunks).strip()


async def list_available_models() -> list[tuple[str, str]]:
    """(id, display_name) pairs for the workshop dropdown, excluding the "auto" meta-model (it
    hides which model actually produced a given commentary, so it's never offered as a choice)."""
    client, _ = await _ensure_client()
    models = await client.list_models()
    return [(m.id, m.name) for m in models if m.id != "auto"]


async def refresh_available_models() -> list[tuple[str, str]]:
    """Force a fresh fetch, busting the SDK's in-process model-list cache. The SDK only clears
    that cache when the client disconnects (no public API invalidates just the cache), so this is
    implemented as a full client restart — which is why it refuses to run while any generation is
    active on the shared client (that would kill its in-flight stream)."""
    if _active_session_count > 0:
        raise RuntimeError("正在生成中，请稍后再刷新模型列表")
    _, generation = await _ensure_client()
    await _stop_shared_client(generation)
    return await list_available_models()
