import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "site" / "index.html"

EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
DASHES = ("\u2014", "\u2013")
NON_ENGLISH_SCRIPT = re.compile("[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0400-\u04ff]")


def public_text_problems(text: str) -> list[str]:
    problems = []
    if any(dash in text for dash in DASHES):
        problems.append("em or en dash")
    if EMOJI.search(text):
        problems.append("emoji")
    if NON_ENGLISH_SCRIPT.search(text):
        problems.append("non-English script")
    lowered = text.lower()
    if "open source" in lowered or "open-source" in lowered:
        problems.append("open source phrase")
    return problems


def test_landing_shell_and_first_half_sections_exist():
    html = INDEX.read_text(encoding="utf-8")
    assert '<html lang="en">' in html
    assert html.count("<h1") == 1
    for section_id in ('id="hero"', 'id="pattern"', 'id="demo"', 'id="evidence"'):
        assert section_id in html
    assert "Explore the demo" in html
    assert "View on GitHub" in html
    assert "Source available for noncommercial use" in html
    assert "Sample data" in html


def test_landing_embeds_the_demo_with_relative_fixture():
    html = INDEX.read_text(encoding="utf-8")
    assert "data-demo-root" in html
    assert 'data-fixture="data/sample-journal.json"' in html
    assert 'src="assets/js/demo.js"' in html
    assert "data-static-fallback" in html
    assert "<noscript>" not in html


def test_landing_images_have_alt_text():
    html = INDEX.read_text(encoding="utf-8")
    imgs = re.findall(r"<img[^>]*>", html)
    assert imgs
    for img in imgs:
        assert "alt=" in img
        alt = re.search(r'alt="([^"]*)"', img)
        assert alt and alt.group(1).strip()


def test_landing_uses_only_base_path_safe_internal_links():
    html = INDEX.read_text(encoding="utf-8")
    assert 'href="/' not in html
    assert 'src="/' not in html


def test_landing_first_half_copy_is_clean_public_english():
    html = INDEX.read_text(encoding="utf-8")
    assert public_text_problems(html) == []
