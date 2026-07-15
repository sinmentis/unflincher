"""The one exact prepared-request envelope shared by context-budget preflight AND real SDK
generation (see llm.stream_completion). Building this in exactly one place is the whole point:
a preflight check that assembled its own approximation of "what the model will receive" could
diverge from the real call and let an oversized request slip through, or reject a request the
model would actually have accepted. Every generation path (Entry Reflection, Life Report, Prompt
Workshop preview, entry/general conversation, title generation, background-job items) must build
its envelope through build_envelope() below, and stream_completion() must derive its SDK kwargs
FROM the envelope's fields rather than reconstructing them separately.

The envelope is also what background-job items fingerprint at enqueue time (see
db.enqueue_snapshot_regen_job): retry/recovery reconstruct it from the immutable prompt and
ordered entry snapshot, then require both ASSEMBLY_VERSION and the fingerprint to match before
ever calling the model again. A code change to formatting, task instructions, session options, or
assembly is exactly a change to this module (or how a caller builds an envelope) and must bump
ASSEMBLY_VERSION so stale in-flight work is refused rather than silently regenerated under a
different contract.
"""
import hashlib
import json
from dataclasses import dataclass, field

# Bump whenever a code change alters what gets sent to the model for a given (system, user_content,
# model, target) tuple: formatting, task instructions, session options (working_directory, tool
# allowlist, discovery flags, streaming mode), or this envelope's own shape. Retry/recovery compare
# this against the value stored at enqueue time (see db.enqueue_snapshot_regen_job) and refuse to
# resume stale work under a changed contract (stable 409 request_format_changed).
ASSEMBLY_VERSION = 1

# Fixed, security-relevant session options every generation path uses. Never overridden per-call:
# available_tools=() disables the whole tool catalog (no filesystem/network access for the model),
# working_directory="/tmp" and the discovery flags below isolate from any ambient
# AGENTS.md/.github/copilot-instructions on the host. See llm.stream_completion's own docstring for
# the full rationale; this module is the single place that rationale is encoded as data.
_AVAILABLE_TOOLS: tuple[str, ...] = ()
_WORKING_DIRECTORY = "/tmp"
_SKIP_CUSTOM_INSTRUCTIONS = True
_ENABLE_CONFIG_DISCOVERY = False
_ENABLE_SKILLS = False
_STREAMING = True
_SYSTEM_MODE = "replace"


@dataclass(frozen=True)
class RequestEnvelope:
    """The exact object whose fields become the SDK call. Nothing about the SDK-visible request
    may be reconstructed from anywhere else once this envelope exists -- see module docstring."""

    assembly_version: int
    system_mode: str
    system_content: str
    user_content: str
    model: str
    available_tools: tuple[str, ...]
    working_directory: str
    skip_custom_instructions: bool
    enable_config_discovery: bool
    enable_skills: bool
    streaming: bool
    # Identifies WHAT this request is for, so an activity meant for one target (e.g. entry 12's
    # reflection) can never be silently confused with another (e.g. the life report) even though
    # both may share identical system/user text in a pathological case.
    target_kind: str
    target_id: str | None = field(default=None)


def build_envelope(
    system: str,
    user_content: str,
    model: str,
    *,
    target_kind: str,
    target_id: str | int | None = None,
) -> RequestEnvelope:
    """Build the one envelope used both for preflight estimation and the real SDK call.

    target_kind/target_id are metadata only (never sent to the model) -- they let a fingerprint
    distinguish "this exact text for entry 12" from "the same text for the life report" and let
    a caller log/report which target an oversized-request error came from.
    """
    return RequestEnvelope(
        assembly_version=ASSEMBLY_VERSION,
        system_mode=_SYSTEM_MODE,
        system_content=system,
        user_content=user_content,
        model=model,
        available_tools=_AVAILABLE_TOOLS,
        working_directory=_WORKING_DIRECTORY,
        skip_custom_instructions=_SKIP_CUSTOM_INSTRUCTIONS,
        enable_config_discovery=_ENABLE_CONFIG_DISCOVERY,
        enable_skills=_ENABLE_SKILLS,
        streaming=_STREAMING,
        target_kind=target_kind,
        target_id=str(target_id) if target_id is not None else None,
    )


def canonical_json(envelope: RequestEnvelope) -> str:
    """Stable, deterministic serialization of every SDK-visible/session field plus the target
    identity and assembly version -- the exact input to fingerprint(). Sorted keys and a fixed
    separator style so the same envelope always serializes identically across processes and
    Python versions; this is a durable, persisted format (stored fingerprints must remain
    reproducible), not a debugging convenience."""
    payload = {
        "assembly_version": envelope.assembly_version,
        "system_mode": envelope.system_mode,
        "system_content": envelope.system_content,
        "user_content": envelope.user_content,
        "model": envelope.model,
        "available_tools": list(envelope.available_tools),
        "working_directory": envelope.working_directory,
        "skip_custom_instructions": envelope.skip_custom_instructions,
        "enable_config_discovery": envelope.enable_config_discovery,
        "enable_skills": envelope.enable_skills,
        "streaming": envelope.streaming,
        "target_kind": envelope.target_kind,
        "target_id": envelope.target_id,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def fingerprint(envelope: RequestEnvelope) -> str:
    """SHA-256 hex digest of canonical_json(envelope) -- the value stored per regen_job_item at
    enqueue time and re-derived (from the immutable prompt + ordered entry snapshot) at retry and
    crash-recovery time. A mismatch means the code that assembles requests changed since this item
    was admitted; see this module's docstring."""
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


def matches_stored_assembly(
    envelope: RequestEnvelope, *, stored_assembly_version: int, stored_fingerprint: str
) -> bool:
    """True only if a freshly rebuilt envelope's assembly_version AND fingerprint both equal what
    was persisted at enqueue time (see db.PreparedRegenTarget/db.retry_failed_job_item). Retry and
    crash recovery must call this after reconstructing the envelope from the immutable prompt and
    ordered entry snapshot, and refuse to call the model (stable 409 request_format_changed) when
    it returns False -- a mismatch means the code that assembles requests changed since this item
    was admitted, so the persisted fingerprint no longer describes what would actually be sent."""
    return (
        envelope.assembly_version == stored_assembly_version
        and fingerprint(envelope) == stored_fingerprint
    )
