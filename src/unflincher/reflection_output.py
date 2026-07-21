import re
from dataclasses import dataclass


_SCORE_MARKER = "[wellbeing-score]:"
_SCORE_RE = re.compile(
    r"(?:\r?\n){1,3}\[wellbeing-score\]:\s*#\s*\"(?P<score>\d{1,3})\"\s*\Z"
)


class InvalidReflectionOutput(ValueError):
    pass


@dataclass(frozen=True)
class ReflectionOutput:
    body_text: str
    wellbeing_score: int | None


def parse_reflection_output(text: str, *, require_score: bool = False) -> ReflectionOutput:
    match = _SCORE_RE.search(text)
    if match is None:
        if require_score:
            if _SCORE_MARKER in text.lower():
                raise InvalidReflectionOutput(
                    "Entry Reflection has malformed wellbeing score metadata"
                )
            raise InvalidReflectionOutput("Entry Reflection is missing wellbeing score metadata")
        return ReflectionOutput(body_text=text, wellbeing_score=None)

    body_text = text[:match.start()].rstrip()
    score = int(match.group("score"))
    if not 0 <= score <= 100:
        if require_score:
            raise InvalidReflectionOutput(
                "Entry Reflection wellbeing score must be between 0 and 100"
            )
        return ReflectionOutput(body_text=body_text or text, wellbeing_score=None)

    if not body_text:
        if require_score:
            raise InvalidReflectionOutput("Entry Reflection body is empty")
        return ReflectionOutput(body_text=text, wellbeing_score=None)
    return ReflectionOutput(body_text=body_text, wellbeing_score=score)
