import re
from pathlib import Path

import pytest

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


import json
import shutil
import subprocess

SITE_JS = ROOT / "site" / "assets" / "js" / "site.js"
LANDING_CSS = ROOT / "site" / "assets" / "css" / "landing.css"


def _run_site_node(source: str) -> str:
    return subprocess.run(
        ["node", "-e", source, str(SITE_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_landing_second_half_sections_exist():
    html = INDEX.read_text(encoding="utf-8")
    for section_id in ('id="conversation"', 'id="workshop"', 'id="privacy"', 'id="cta"'):
        assert section_id in html
    assert "<!-- LANDING-PART-2" not in html
    assert ">Local<" in html
    assert "External processing" in html
    assert "GitHub Pages" in html
    assert "platform logging and privacy practices" in html


def test_landing_loads_the_reveal_script():
    html = INDEX.read_text(encoding="utf-8")
    assert 'src="assets/js/site.js"' in html


def test_landing_is_responsive_and_reduced_motion_aware():
    css = LANDING_CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 768px)" in css
    assert "grid-template-columns: 1fr" in css
    js = SITE_JS.read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in js


def test_full_landing_copy_is_clean_public_english():
    assert public_text_problems(INDEX.read_text(encoding="utf-8")) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_prefers_reduced_motion_reads_matchmedia():
    output = _run_site_node(
        "const {prefersReducedMotion} = require(process.argv[1]);"
        "const on = {matchMedia: () => ({matches: true})};"
        "const off = {matchMedia: () => ({matches: false})};"
        "process.stdout.write(JSON.stringify({on: prefersReducedMotion(on), off: prefersReducedMotion(off), none: prefersReducedMotion({})}));"
    )
    assert json.loads(output) == {"on": True, "off": False, "none": False}


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_reduced_motion_reveals_all_content_without_observer():
    output = _run_site_node(
        "const {revealOnScroll} = require(process.argv[1]);"
        "const makeTarget = () => ({classList:{added:[],add(v){this.added.push(v)}}});"
        "const targets = [makeTarget(), makeTarget()];"
        "const doc = {querySelectorAll: () => targets};"
        "const win = {matchMedia: () => ({matches: true})};"
        "revealOnScroll(doc, win);"
        "process.stdout.write(JSON.stringify(targets.map(t => t.classList.added)));"
    )
    assert json.loads(output) == [["is-revealed"], ["is-revealed"]]
