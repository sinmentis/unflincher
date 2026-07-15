import pytest

from unflincher import perspectives


EXPECTED_KEYS = ("companion", "coach", "challenger", "analyst")


def test_catalog_has_stable_order_and_analyst_default():
    assert perspectives.PERSPECTIVE_KEYS == EXPECTED_KEYS
    assert perspectives.DEFAULT_PERSPECTIVE_KEY == "analyst"
    assert tuple(preset.key for preset in perspectives.list_presets()) == EXPECTED_KEYS


def test_each_preset_has_unique_translation_keys_and_prompt():
    presets = perspectives.list_presets()
    assert len({preset.name_key for preset in presets}) == len(presets)
    assert len({preset.description_key for preset in presets}) == len(presets)
    assert len({preset.prompt for preset in presets}) == len(presets)
    for preset in presets:
        assert preset.name_key == f"perspective.{preset.key}.name"
        assert preset.description_key == f"perspective.{preset.key}.description"


@pytest.mark.parametrize("key", EXPECTED_KEYS)
def test_every_prompt_keeps_the_shared_reflection_contract(key):
    prompt = perspectives.get_preset(key).prompt
    required = (
        "specific journal entries",
        "dated entry references",
        "current conversation",
        "not dated archive evidence",
        "observation from interpretation",
        "state uncertainty",
        "inferred motives",
        "language used by the journal owner",
        "Do not diagnose",
        "Do not claim to be human",
        "Critique patterns, choices, and behavior",
    )
    for phrase in required:
        assert phrase in prompt


def test_presets_have_distinct_behavioral_stances():
    assert "acknowledge the emotional reality" in perspectives.get_preset("companion").prompt
    assert "smallest supported next step" in perspectives.get_preset("coach").prompt
    challenger = perspectives.get_preset("challenger").prompt
    assert "Name contradictions, avoidance, and moving excuses directly" in challenger
    assert "only when the entries strongly support those interpretations" in challenger
    assert "minimum necessary editorializing" in perspectives.get_preset("analyst").prompt


def test_prompt_classification_requires_an_exact_catalog_match():
    analyst = perspectives.get_preset("analyst").prompt
    assert perspectives.classify_prompt(analyst) == "analyst"
    assert perspectives.classify_prompt(f"{analyst}\n") is None
    assert perspectives.classify_prompt("Write however you want.") is None


def test_unknown_preset_key_is_rejected():
    with pytest.raises(KeyError):
        perspectives.get_preset("therapist")


def test_prompts_avoid_false_intimacy_and_identity_attacks():
    forbidden = (
        "I love you",
        "I care about you",
        "I will always be here",
        "you are a coward",
        "you are broken",
        "humiliate",
    )
    for preset in perspectives.list_presets():
        for phrase in forbidden:
            assert phrase.lower() not in preset.prompt.lower()
