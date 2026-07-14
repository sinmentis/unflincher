"""Validator for the public synthetic demo fixture (site/data/sample-journal.json).

Pure stdlib. Returns a list of human-readable error strings; an empty list means the
fixture is safe to publish. Reused by tests/test_public_fixture.py and the public
readiness audit (tools/public_readiness_audit.py)."""
from __future__ import annotations

import datetime as _dt

VIEW_KEYS = ("timeline", "entry", "report", "conversation", "workshop")


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
        for field in ("title", "body", "commentary"):
            if not _nonempty_str(entry.get(field)):
                errors.append(f"entries[{index}].{field} must be a non-empty string")

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
            if message.get("role") not in {"user", "assistant"}:
                errors.append(
                    f"conversation.messages[{index}].role must be user or assistant"
                )

    workshop = data.get("workshop")
    if not isinstance(workshop, dict):
        errors.append("workshop must be an object")
    else:
        personas = workshop.get("personas")
        if not isinstance(personas, list) or len(personas) < 2:
            errors.append("workshop.personas must have at least two personas")
            personas = []
        for index, persona in enumerate(personas):
            if not isinstance(persona, dict):
                errors.append(f"workshop.personas[{index}] must be an object")
                continue
            for field in ("name", "prompt", "sample"):
                if not _nonempty_str(persona.get(field)):
                    errors.append(f"workshop.personas[{index}].{field} must be a non-empty string")

    return errors


def main(argv=None) -> int:
    import json
    import sys
    from pathlib import Path

    argv = list(sys.argv[1:] if argv is None else argv)
    path = Path(argv[0]) if argv else Path("site/data/sample-journal.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = validate_fixture(data)
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
