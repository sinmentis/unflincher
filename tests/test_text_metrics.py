from unflincher.text_metrics import count_writing_units


def test_count_writing_units_handles_latin_cjk_and_punctuation():
    assert count_writing_units("") == 0
    assert count_writing_units("a quiet morning") == 3
    assert count_writing_units("今天心情很好。") == 6
    assert count_writing_units("今天 feels calm") == 4
    assert count_writing_units("well-being isn't lost") == 3


def test_count_writing_units_handles_japanese_and_korean_without_spaces():
    assert count_writing_units("静かな朝") == 4
    assert count_writing_units("좋은아침") == 4
