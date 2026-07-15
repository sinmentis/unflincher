"""Context-capacity contract: the long-term-archive promise ("Unflincher reads across your whole
journal archive") needs an explicit, tested boundary instead of an implicit hope that requests
stay small. This module estimates the size of one prepared RequestEnvelope (see
request_envelope.py) against a model's published max_prompt_tokens and raises a stable error
BEFORE any SSE stream opens or any durable write happens -- never truncates, samples, or drops
older entries to make a request fit (see the plan's Context budget and failure contract).

The estimator is intentionally dependency-free (no tokenizer library) and intentionally
conservative: it is tuned to OVER-estimate token counts so a request that would in fact have
fit is rejected only in rare edge cases, while a request that would in fact overflow the model's
window is essentially never underestimated as safe. Getting this exactly right requires the
model's own tokenizer, which is not available locally; the SAFETY_MARGIN_RATIO on top absorbs
protocol/formatting overhead (system-message wrapping, session metadata) the estimator does not
model at all.
"""
from dataclasses import dataclass

# Rough, deliberately pessimistic bytes-per-token ratio. Common English BPE tokenizers average
# roughly 4 bytes/token; CJK text (this app's primary content language) tends to run considerably
# denser per character under byte-level BPE (each Han character is 3 UTF-8 bytes and merges less
# efficiently than Latin text), so a 2-bytes/token ratio stays conservative across both scripts
# without needing per-language branching. Tests pin this constant directly so a future change is a
# deliberate, reviewed decision, not an accidental drift.
BYTES_PER_TOKEN_ESTIMATE = 2.0

# Extra headroom for protocol/session overhead the estimator does not itself model: the system-
# message wrapper, session bookkeeping, and any fixed per-request scaffolding the SDK/CLI adds.
# Applied multiplicatively on top of the raw estimate.
SAFETY_MARGIN_RATIO = 1.15


class ModelLimitsUnavailableError(RuntimeError):
    """The selected model's max_prompt_tokens capability could not be obtained (model-list fetch
    failed, or the model advertises no prompt-token limit). Retryable (maps to a stable 503
    model_limits_unavailable) -- callers must never guess a limit or silently continue without
    one."""

    def __init__(self, model: str, reason: str = ""):
        self.model = model
        self.reason = reason
        message = f"max_prompt_tokens unavailable for model {model!r}"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


class ContextTooLargeError(RuntimeError):
    """The prepared request's estimated size exceeds the selected model's max_prompt_tokens once
    the safety margin is applied. Maps to a stable 413 context_too_large -- callers must raise
    this BEFORE opening an SSE stream or making any durable domain write (session/message insert,
    prompt activation, job enqueue). Carries enough detail for the eventual route layer to show
    the estimated size and the model's limit, plus suggested actions (larger-context model, or
    reduce the archive/history)."""

    def __init__(self, *, model: str, estimated_tokens: int, limit: int, target_kind: str, target_id: str | None = None):
        self.model = model
        self.estimated_tokens = estimated_tokens
        self.limit = limit
        self.target_kind = target_kind
        self.target_id = target_id
        super().__init__(
            f"estimated request size {estimated_tokens} tokens exceeds model {model!r} "
            f"max_prompt_tokens={limit} (target={target_kind}:{target_id})"
        )


@dataclass(frozen=True)
class BudgetCheck:
    """Result of a successful preflight_envelope() call -- returned (not raised) so a caller can
    log/display the estimate even when the request is within budget."""

    estimated_tokens: int
    limit: int
    margin_ratio: float


def estimate_tokens(text: str) -> int:
    """Dependency-free, conservative token estimate for one piece of text. See module docstring
    for the bytes-per-token rationale. Always returns at least 1 for non-empty text so an empty
    string is the only input that estimates to zero."""
    if not text:
        return 0
    byte_length = len(text.encode("utf-8"))
    return max(1, -(-int(byte_length) // int(BYTES_PER_TOKEN_ESTIMATE)) if byte_length else 0)


def estimate_envelope_tokens(envelope) -> int:
    """Estimate the FULL prepared request: system content plus user content, since both are
    genuinely sent to the model as prompt tokens. Anything added to RequestEnvelope in the future
    that is also sent as prompt text must be added here too -- this function, not ad-hoc route
    code, is the single place "how big is this request" is computed."""
    return estimate_tokens(envelope.system_content) + estimate_tokens(envelope.user_content)


def preflight_envelope(envelope, max_prompt_tokens: int) -> BudgetCheck:
    """Raise ContextTooLargeError if the envelope's estimated size (with safety margin) exceeds
    max_prompt_tokens; otherwise return a BudgetCheck describing the estimate. Callers must obtain
    max_prompt_tokens via a current, real model-capability lookup (see
    llm.get_model_max_prompt_tokens) -- never guess or reuse a stale/hardcoded number."""
    if max_prompt_tokens is None or max_prompt_tokens <= 0:
        raise ModelLimitsUnavailableError(envelope.model, "non-positive or missing limit")
    raw_estimate = estimate_envelope_tokens(envelope)
    margined_estimate = int(-(-int(raw_estimate * SAFETY_MARGIN_RATIO) // 1))
    if margined_estimate > max_prompt_tokens:
        raise ContextTooLargeError(
            model=envelope.model,
            estimated_tokens=margined_estimate,
            limit=max_prompt_tokens,
            target_kind=envelope.target_kind,
            target_id=envelope.target_id,
        )
    return BudgetCheck(
        estimated_tokens=margined_estimate, limit=max_prompt_tokens, margin_ratio=SAFETY_MARGIN_RATIO
    )
