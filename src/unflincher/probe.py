"""Local-only synthetic deployment probe: proves the shared Copilot client, context-budget
preflight, and request-envelope assembly all work in a freshly deployed container WITHOUT
opening user generation.

Callable only through local container execution (`python -m unflincher.cli probe`) -- there is no
HTTP route for this. Performs NO database access of any kind (this module never imports
unflincher.db and never opens a database connection), uses one fixed, built-in synthetic request,
and exits after exactly one response.

This is one of exactly two allowed maintenance bypasses in the eventual design (see the plan's
Maintenance gate section and db.MaintenanceLockedError's docstring). It never activates a prompt,
creates a job, reads the private Journal Archive, or acquires a normal generation lease -- it
simply proves the Copilot client/model/context-budget path is healthy while maintenance is
locked, as one step of the deploy procedure."""
from unflincher import llm
from unflincher.context_budget import preflight_envelope
from unflincher.request_envelope import build_envelope

# Fixed, synthetic, non-sensitive content -- never derived from any journal entry, prompt, or
# other private data. Kept short so the probe stays fast and its context-budget preflight is
# trivially satisfied by any supported model.
PROBE_SYSTEM_PROMPT = (
    "You are a local, non-persisting deployment health probe. This is not a real user request "
    "and nothing about it should be treated as private or actionable."
)
PROBE_USER_MESSAGE = "Reply with exactly one word: ok"

PROBE_TARGET_KIND = "deploy_probe"


async def run_probe(model: str) -> str:
    """Build the synthetic envelope, preflight it against the model's CURRENT published
    max_prompt_tokens, then stream that EXACT SAME envelope object (never rebuilt from strings)
    through generation and return the model's full reply text (stripped). Raises
    context_budget.ModelLimitsUnavailableError, context_budget.ContextTooLargeError,
    llm.ModelSessionError, or a transport error exactly like any other generation path -- the
    caller (cli.py) turns any of those into a non-zero process exit rather than swallowing them."""
    envelope = build_envelope(
        PROBE_SYSTEM_PROMPT, PROBE_USER_MESSAGE, model, target_kind=PROBE_TARGET_KIND
    )
    limit = await llm.get_model_max_prompt_tokens(model)
    preflight_envelope(envelope, limit)

    chunks = [token async for token in llm.stream_completion_envelope(envelope)]
    return "".join(chunks).strip()
