import pytest

from unflincher.reflection_output import (
    InvalidReflectionOutput,
    parse_reflection_output,
)


def test_parse_reflection_output_separates_score_metadata():
    parsed = parse_reflection_output(
        'A grounded reflection.\n\n[wellbeing-score]: # "73"',
        require_score=True,
    )

    assert parsed.body_text == "A grounded reflection."
    assert parsed.wellbeing_score == 73


def test_parse_reflection_output_accepts_legacy_unscored_reflections():
    parsed = parse_reflection_output("Legacy reflection.")

    assert parsed.body_text == "Legacy reflection."
    assert parsed.wellbeing_score is None


def test_parse_reflection_output_accepts_extra_space_before_score_metadata():
    parsed = parse_reflection_output(
        'A grounded reflection.\n\n\n\n[wellbeing-score]: # "73"',
        require_score=True,
    )

    assert parsed.body_text == "A grounded reflection."
    assert parsed.wellbeing_score == 73


def test_parse_reflection_output_keeps_malformed_legacy_reflections_readable():
    parsed = parse_reflection_output("Legacy reflection.\n\n[wellbeing-score]: not-a-score")

    assert parsed.body_text == "Legacy reflection.\n\n[wellbeing-score]: not-a-score"
    assert parsed.wellbeing_score is None


def test_parse_reflection_output_hides_out_of_range_legacy_score_metadata():
    parsed = parse_reflection_output('Legacy reflection.\n\n[wellbeing-score]: # "101"')

    assert parsed.body_text == "Legacy reflection."
    assert parsed.wellbeing_score is None


@pytest.mark.parametrize(
    "body",
    [
        "Missing score.",
        'Out of range.\n\n[wellbeing-score]: # "101"',
        '[wellbeing-score]: # "50"',
        'Malformed.\n\n[wellbeing-score]: 50',
    ],
)
def test_parse_reflection_output_rejects_invalid_scored_results(body):
    with pytest.raises(InvalidReflectionOutput):
        parse_reflection_output(body, require_score=True)
