"""Validator for the public synthetic demo fixture (site/data/sample-journal.json).

Pure stdlib. Returns a list of human-readable error strings; an empty list means the
fixture is safe to publish. Reused by tests/test_public_fixture.py and the public
readiness audit (tools/public_readiness_audit.py)."""
from __future__ import annotations

import datetime as _dt
import importlib
import re
import sys
from itertools import combinations
from pathlib import Path

VIEW_KEYS = ("timeline", "entry", "report", "conversation", "workshop")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_i18n_module = importlib.import_module("unflincher.i18n")
_perspectives_module = importlib.import_module("unflincher.perspectives")
_i18n_t = _i18n_module.t
_CANONICAL_PERSPECTIVE_KEYS = _perspectives_module.PERSPECTIVE_KEYS
_get_canonical_preset = _perspectives_module.get_preset

WORKSHOP_PERSPECTIVE_KEYS = tuple(_CANONICAL_PERSPECTIVE_KEYS) + ("custom",)
_DATE_REFERENCE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_MAX_READING_JACCARD = 0.8

# Focused, non-exhaustive phrase patterns that a public reflection-partner fixture must
# never contain: clinical/medical impersonation, treatment claims, humiliation/identity
# attacks, and false intimacy. Grouped by label so validator errors name the category.
_UNSAFE_PATTERN_GROUPS = (
    (
        "diagnosis",
        (
            re.compile(
                r"\byou (?:have|are diagnosed with) (?:a |an )?(?:clinical )?"
                r"(?:depression|anxiety disorder|ptsd|bipolar disorder|bpd|ocd)\b",
                re.IGNORECASE,
            ),
            re.compile(r"\bclinical(?:ly)? diagnos\w*\b", re.IGNORECASE),
            re.compile(r"\bdiagnosis:\s", re.IGNORECASE),
        ),
    ),
    (
        "clinical-role",
        (
            re.compile(
                r"\b(?:as|i am) your (?:therapist|doctor|psychiatrist|psychologist|"
                r"counselor|physician)\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "treatment",
        (
            re.compile(r"\btreatment plan\b", re.IGNORECASE),
            re.compile(r"\bprescri\w*\b", re.IGNORECASE),
            re.compile(r"\bmedicat(?:e|ion|ing)\b", re.IGNORECASE),
            re.compile(r"\bcure (?:you|your)\b", re.IGNORECASE),
        ),
    ),
    (
        "humiliation",
        (
            re.compile(
                r"\byou(?:'re| are) (?:worthless|pathetic|a failure|stupid|broken|"
                r"a fraud|a disappointment)\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "false-intimacy",
        (
            re.compile(r"\bi love you\b", re.IGNORECASE),
            re.compile(r"\bi need you\b", re.IGNORECASE),
            re.compile(r"\bi will always be (?:here|with you)\b", re.IGNORECASE),
            re.compile(r"\bi will never leave you\b", re.IGNORECASE),
            re.compile(r"\byou(?:'re| are) my (?:best friend|only friend)\b", re.IGNORECASE),
        ),
    ),
)


def find_unsafe_phrases(label: str, text: str) -> list[str]:
    issues: list[str] = []
    if not isinstance(text, str) or not text:
        return issues
    for category, patterns in _UNSAFE_PATTERN_GROUPS:
        for pattern in patterns:
            if pattern.search(text):
                issues.append(f"{label}: disallowed {category} phrase")
    return issues


def _is_iso_date(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        _dt.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _reading_similarity(left: str, right: str) -> float:
    left_words = set(_WORD_RE.findall(left.lower()))
    right_words = set(_WORD_RE.findall(right.lower()))
    union = left_words | right_words
    if not union:
        return 1.0
    return len(left_words & right_words) / len(union)


def validate_fixture(data) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["fixture root must be an object"]

    meta = data.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta must be an object")
    elif meta.get("synthetic") is not True:
        errors.append("meta.synthetic must be true")

    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("entries must be a non-empty list")
        entries = []

    ids: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entries[{index}] must be an object")
            continue
        entry_id = entry.get("id")
        if not _nonempty_str(entry_id):
            errors.append(f"entries[{index}].id must be a non-empty string")
        else:
            ids.append(entry_id)
        if not _is_iso_date(entry.get("date")):
            errors.append(f"entries[{index}].date must be an ISO date")
        for field in ("title", "body", "reflection"):
            if not _nonempty_str(entry.get(field)):
                errors.append(f"entries[{index}].{field} must be a non-empty string")
            else:
                errors += find_unsafe_phrases(f"entries[{index}].{field}", entry.get(field))

    if len(ids) != len(set(ids)):
        errors.append("entry ids must be unique")

    years = {
        entry.get("date", "")[:4]
        for entry in entries
        if isinstance(entry, dict) and _is_iso_date(entry.get("date"))
    }
    if len({year for year in years if year}) < 3:
        errors.append("entries must span at least three distinct years")

    id_set = set(ids)
    entry_dates = {
        entry.get("date")
        for entry in entries
        if isinstance(entry, dict) and _is_iso_date(entry.get("date"))
    }
    report = data.get("report")
    if not isinstance(report, dict):
        errors.append("report must be an object")
    else:
        sections = report.get("sections")
        if not isinstance(sections, list) or not sections:
            errors.append("report.sections must be a non-empty list")
            sections = []
        cited = False
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                errors.append(f"report.sections[{index}] must be an object")
                continue
            for field in ("heading", "body"):
                if not _nonempty_str(section.get(field)):
                    errors.append(f"report.sections[{index}].{field} must be a non-empty string")
                else:
                    errors += find_unsafe_phrases(
                        f"report.sections[{index}].{field}", section.get(field)
                    )
            evidence = section.get("evidence", [])
            if not isinstance(evidence, list):
                errors.append(f"report.sections[{index}].evidence must be a list")
                evidence = []
            if evidence:
                cited = True
            for ref in evidence:
                if ref not in id_set:
                    errors.append(
                        f"report.sections[{index}].evidence references unknown entry id {ref!r}"
                    )
        if sections and not cited:
            errors.append("report must cite at least one entry as evidence")

    conversation = data.get("conversation")
    if not isinstance(conversation, dict):
        errors.append("conversation must be an object")
    else:
        messages = conversation.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            errors.append("conversation.messages must have at least two messages")
            messages = []
        roles = {m.get("role") for m in messages if isinstance(m, dict)}
        if not {"user", "assistant"}.issubset(roles):
            errors.append("conversation must include both a user and an assistant message")
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or not _nonempty_str(message.get("text")):
                errors.append(f"conversation.messages[{index}].text must be a non-empty string")
                continue
            errors += find_unsafe_phrases(
                f"conversation.messages[{index}].text", message.get("text")
            )
            if message.get("role") not in {"user", "assistant"}:
                errors.append(
                    f"conversation.messages[{index}].role must be user or assistant"
                )

    workshop = data.get("workshop")
    if not isinstance(workshop, dict):
        errors.append("workshop must be an object")
    else:
        workshop_entry_id = workshop.get("entry_id")
        workshop_entry_date = None
        if not _nonempty_str(workshop_entry_id) or workshop_entry_id not in id_set:
            errors.append("workshop.entry_id must reference an existing entry id")
        else:
            workshop_entry_date = next(
                (
                    entry.get("date")
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("id") == workshop_entry_id
                ),
                None,
            )

        perspectives = workshop.get("perspectives")
        expected_keys = WORKSHOP_PERSPECTIVE_KEYS
        if not isinstance(perspectives, list) or len(perspectives) != len(expected_keys):
            errors.append(
                f"workshop.perspectives must be a list of exactly {len(expected_keys)} items "
                f"in the order {expected_keys!r}"
            )
            perspectives = []

        preset_names: set[str] = set()
        preset_prompts: set[str] = set()
        preset_names = {
            _i18n_t("en", f"perspective.{key}.name") for key in _CANONICAL_PERSPECTIVE_KEYS
        }
        preset_prompts = {
            _get_canonical_preset(key).prompt for key in _CANONICAL_PERSPECTIVE_KEYS
        }

        readings: list[str] = []
        for index, expected_key in enumerate(expected_keys):
            persp = perspectives[index] if index < len(perspectives) else None
            if not isinstance(persp, dict):
                errors.append(f"workshop.perspectives[{index}] must be an object")
                continue
            if persp.get("key") != expected_key:
                errors.append(
                    f"workshop.perspectives[{index}].key must be {expected_key!r}"
                )
            for field in ("name", "instructions", "reading"):
                if not _nonempty_str(persp.get(field)):
                    errors.append(
                        f"workshop.perspectives[{index}].{field} must be a non-empty string"
                    )
                else:
                    errors += find_unsafe_phrases(
                        f"workshop.perspectives[{index}].{field}", persp.get(field)
                    )

            name = persp.get("name")
            instructions = persp.get("instructions")
            reading = persp.get("reading")

            if expected_key != "custom":
                canonical_name = _i18n_t("en", f"perspective.{expected_key}.name")
                if name != canonical_name:
                    errors.append(
                        f"workshop.perspectives[{index}].name must match the canonical "
                        f"Perspective name {canonical_name!r}"
                    )
                canonical_prompt = _get_canonical_preset(expected_key).prompt
                if instructions != canonical_prompt:
                    errors.append(
                        f"workshop.perspectives[{index}].instructions must match the "
                        "canonical Perspective prompt text exactly"
                    )
            else:
                if isinstance(name, str) and name in preset_names:
                    errors.append(
                        "workshop.perspectives[4] (Custom) name must not impersonate a "
                        "shipped Perspective preset"
                    )
                if isinstance(instructions, str) and instructions in preset_prompts:
                    errors.append(
                        "workshop.perspectives[4] (Custom) instructions must not exactly "
                        "match a shipped Perspective preset"
                    )

            if isinstance(reading, str) and reading:
                readings.append(reading)
                if workshop_entry_date and workshop_entry_date not in reading:
                    errors.append(
                        f"workshop.perspectives[{index}].reading must reference the dated "
                        f"entry evidence {workshop_entry_date!r}"
                    )
                for unknown_date in sorted(set(_DATE_REFERENCE_RE.findall(reading)) - entry_dates):
                    errors.append(
                        f"workshop.perspectives[{index}].reading references unknown "
                        f"entry date {unknown_date!r}"
                    )

        if any(
            _reading_similarity(left, right) >= _MAX_READING_JACCARD
            for left, right in combinations(readings, 2)
        ):
            errors.append(
                "workshop.perspectives readings must be recognizably distinct across "
                "Perspectives"
            )

    return errors


def main(argv=None) -> int:
    import json

    argv = list(sys.argv[1:] if argv is None else argv)
    path = Path(argv[0]) if argv else Path("site/data/sample-journal.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_fixture(data)
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
