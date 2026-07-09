"""LLM orchestration. Every generation path (real per-entry commentary, aggregate report,
both chat surfaces, and the prompt-workshop test-run preview) goes through the SAME
generate_* functions here with the SAME context-assembly logic — this module never writes
to the database. Persisting a generation (or not, for test-run) is entirely the caller's
responsibility (see Task 9's route and Task 13's worker vs. Task 12's test-run route)."""
import asyncio
from collections.abc import AsyncIterator

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


def ensure_default_persona_prompt(conn) -> None:
    """Seed the default persona on first startup — a no-op if a version already exists."""
    from diary.db import get_active_prompt, set_active_prompt
    if get_active_prompt(conn) is None:
        set_active_prompt(conn, DEFAULT_PERSONA_PROMPT)


async def stream_completion(system: str, user_content: str, model: str) -> AsyncIterator[str]:
    """Stream one completion token-by-token via the GitHub Copilot SDK. This is the ONLY
    place the whole app talks to an LLM; every generate_*/chat_* path funnels through here.
    Authenticates with COPILOT_GITHUB_TOKEN (a shared fine-grained GitHub PAT injected by the
    Quadlet unit) — CopilotClient() auto-detects that env var, so no key is passed in code."""
    from copilot import CopilotClient
    from copilot.generated.session_events import (
        AssistantIdleData,
        AssistantMessageDeltaData,
        SessionErrorData,
    )

    # The SDK dispatches session events from its background JSON-RPC reader thread, not from
    # this coroutine's event loop. Bridge them onto the loop via call_soon_threadsafe — calling
    # queue.put_nowait directly from the handler would be an unsafe cross-thread touch of
    # asyncio internals.
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _on_event(event):
        loop.call_soon_threadsafe(queue.put_nowait, event)

    client = CopilotClient()
    unsubscribe = None
    try:
        await client.start()
        session = await client.create_session(
            model=model,
            # "replace" mode: use diary's own persona+task text verbatim as the system prompt.
            # The SDK's default is its full general-purpose coding-agent system prompt
            # (identity/tone/tool/environment/code-change sections); that would contaminate the
            # life-mentor persona. Do NOT simplify this back to the SDK default.
            system_message={"mode": "replace", "content": system},
            # Pure text-in/text-out. An empty allowlist disables the entire merged tool catalog
            # (built-in + MCP + custom) so the model can never touch this server's filesystem or
            # network. Security requirement, not an optimization — keep it empty.
            available_tools=[],
            # Isolate from any ambient AGENTS.md/.github/copilot-instructions on the host: custom
            # instruction files load based on working_directory regardless of config discovery, so
            # point it at /tmp (guaranteed to exist, guaranteed to hold no instruction files) and
            # belt-and-suspenders the rest off.
            working_directory="/tmp",
            skip_custom_instructions=True,
            enable_config_discovery=False,
            enable_skills=False,
            streaming=True,
            on_event=_on_event,
        )
        # Register once more to capture the unsubscribe callable for guaranteed cleanup. on() adds
        # to a set, so re-registering the same handler is idempotent (it still fires once/event).
        unsubscribe = session.on(_on_event)
        await session.send(user_content)
        while True:
            event = await queue.get()
            data = event.data
            if isinstance(data, AssistantMessageDeltaData):
                if data.delta_content:
                    yield data.delta_content
            elif isinstance(data, SessionErrorData):
                raise RuntimeError(f"Copilot SDK session error: {data.message}")
            elif isinstance(data, AssistantIdleData):
                break
            # Ignore everything else (reasoning/lifecycle/etc.) and keep reading.
    finally:
        if unsubscribe is not None:
            unsubscribe()
        # Unconditional teardown: client.stop() disconnects the session and shuts down the CLI
        # subprocess, so a mid-stream error (or early consumer close) still cleans up.
        await client.stop()


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
