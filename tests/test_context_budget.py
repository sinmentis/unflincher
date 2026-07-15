"""Unit tests for unflincher.context_budget: the dependency-free, conservative estimator and the
stable 413/503 failure contract for the full-archive capacity promise."""
import pytest

from unflincher.context_budget import (
    BudgetCheck,
    ContextTooLargeError,
    ModelLimitsUnavailableError,
    SAFETY_MARGIN_RATIO,
    estimate_envelope_tokens,
    estimate_tokens,
    preflight_envelope,
)
from unflincher.request_envelope import build_envelope


def test_estimate_tokens_empty_string_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_grows_with_length():
    short = estimate_tokens("hello")
    long = estimate_tokens("hello " * 1000)
    assert long > short


def test_estimate_tokens_is_at_least_one_for_nonempty_text():
    assert estimate_tokens("a") >= 1


def test_estimate_tokens_conservative_for_cjk_dense_text():
    # A single Han character is 3 UTF-8 bytes; the conservative bytes-per-token ratio must not
    # under-count it down to a fraction of one token.
    assert estimate_tokens("日") >= 1
    ten_chars = "读日记找模式" * 2  # 12 CJK characters
    assert estimate_tokens(ten_chars) >= 12  # never fewer estimated tokens than characters


def test_estimate_envelope_tokens_sums_system_and_user_content():
    envelope = build_envelope("A" * 100, "B" * 200, "test-model", target_kind="entry_commentary", target_id=1)
    assert estimate_envelope_tokens(envelope) == estimate_tokens("A" * 100) + estimate_tokens("B" * 200)


def test_preflight_envelope_passes_when_within_limit():
    envelope = build_envelope("short", "short", "test-model", target_kind="entry_commentary", target_id=1)
    result = preflight_envelope(envelope, max_prompt_tokens=100_000)
    assert isinstance(result, BudgetCheck)
    assert result.limit == 100_000
    assert result.margin_ratio == SAFETY_MARGIN_RATIO
    assert result.estimated_tokens > 0


def test_preflight_envelope_raises_context_too_large_when_over_limit():
    envelope = build_envelope("x" * 10_000, "y" * 10_000, "test-model", target_kind="aggregate_report")
    with pytest.raises(ContextTooLargeError) as excinfo:
        preflight_envelope(envelope, max_prompt_tokens=10)
    err = excinfo.value
    assert err.model == "test-model"
    assert err.limit == 10
    assert err.estimated_tokens > 10
    assert err.target_kind == "aggregate_report"


def test_preflight_envelope_applies_safety_margin_not_just_raw_estimate():
    # Construct a request whose RAW estimate is within the limit but whose margined estimate
    # exceeds it, proving the margin is actually applied rather than ignored.
    envelope = build_envelope("x" * 1000, "y" * 1000, "test-model", target_kind="entry_commentary", target_id=1)
    raw = estimate_envelope_tokens(envelope)
    limit = raw + 5  # bigger than raw, but smaller than raw * SAFETY_MARGIN_RATIO
    assert limit < raw * SAFETY_MARGIN_RATIO
    with pytest.raises(ContextTooLargeError):
        preflight_envelope(envelope, max_prompt_tokens=limit)


def test_preflight_envelope_raises_model_limits_unavailable_for_missing_limit():
    envelope = build_envelope("s", "u", "test-model", target_kind="entry_commentary", target_id=1)
    with pytest.raises(ModelLimitsUnavailableError) as excinfo:
        preflight_envelope(envelope, max_prompt_tokens=None)
    assert excinfo.value.model == "test-model"


def test_preflight_envelope_raises_model_limits_unavailable_for_non_positive_limit():
    envelope = build_envelope("s", "u", "test-model", target_kind="entry_commentary", target_id=1)
    with pytest.raises(ModelLimitsUnavailableError):
        preflight_envelope(envelope, max_prompt_tokens=0)
    with pytest.raises(ModelLimitsUnavailableError):
        preflight_envelope(envelope, max_prompt_tokens=-5)


def test_context_too_large_error_never_raised_for_model_limits_missing():
    # Regression guard: a missing limit must be its own distinct, retryable error type, never
    # silently coerced into (or confused with) an oversized-request error.
    envelope = build_envelope("s", "u", "test-model", target_kind="entry_commentary", target_id=1)
    with pytest.raises(ModelLimitsUnavailableError):
        preflight_envelope(envelope, max_prompt_tokens=None)
