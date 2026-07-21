import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "site" / "index.html"
SITE_JS = ROOT / "site" / "assets" / "js" / "site.js"
LANDING_CSS = ROOT / "site" / "assets" / "css" / "landing.css"

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


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_landing_shell_and_first_half_sections_exist():
    html = INDEX.read_text(encoding="utf-8")
    assert '<html lang="en">' in html
    assert html.count("<h1") == 1
    for section_id in ('id="hero"', 'id="pattern"', 'id="demo"', 'id="evidence"'):
        assert section_id in html
    assert "AI reflection partner" in html
    assert "An outside perspective on years of your journal." in html
    assert "Explore the fictional demo" in html
    assert "See how it works" in html
    assert "No account and no model call in the demo." in html
    assert "Source available for noncommercial use" in html
    assert "Sample data" in html


def test_landing_leads_with_value_before_setup_or_license():
    html = INDEX.read_text(encoding="utf-8")
    hero = html[html.index('id="hero"') : html.index("</section>", html.index('id="hero"'))]
    assert "Journal Archive" in hero
    assert "recurring patterns" in hero
    assert "dated entries" in hero
    assert "conversation" in hero
    assert "Source available" not in hero
    assert "GitHub Copilot" not in hero


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


def _run_site_node(source: str) -> str:
    return subprocess.run(
        ["node", "-e", source, str(SITE_JS)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_landing_second_half_sections_exist():
    html = INDEX.read_text(encoding="utf-8")
    for section_id in (
        'id="perspectives"',
        'id="conversation"',
        'id="archive"',
        'id="privacy"',
        'id="faq"',
        'id="cta"',
    ):
        assert section_id in html
    assert "<!-- LANDING-PART-2" not in html
    assert "Companion" in html
    assert "Coach" in html
    assert "Challenger" in html
    assert "Analyst" in html
    assert "Custom" in html
    assert "GitHub Pages" in html
    assert "platform logging and privacy practices" in html


def test_landing_sections_follow_the_product_story_order():
    html = INDEX.read_text(encoding="utf-8")
    section_ids = (
        "hero",
        "pattern",
        "demo",
        "evidence",
        "perspectives",
        "conversation",
        "archive",
        "privacy",
        "faq",
        "cta",
    )
    positions = [html.index(f'id="{section_id}"') for section_id in section_ids]
    assert positions == sorted(positions)


def test_landing_faq_is_immediately_before_the_final_action():
    html = INDEX.read_text(encoding="utf-8")
    faq_start = html.index('id="faq"')
    faq_end = html.index("</section>", faq_start) + len("</section>")
    cta_start = html.index('id="cta"')
    cta_tag_start = html.rfind("<section", 0, cta_start)
    assert not html[faq_end:cta_tag_start].strip()
    for question in (
        "What is Unflincher?",
        "What is a Perspective?",
        "What is a Life Report?",
        "What archive can I import?",
        "What leaves my host during generation?",
        "How is Unflincher licensed?",
        "Is Unflincher a substitute for therapy?",
    ):
        assert question in html[faq_start:faq_end]


def test_landing_perspectives_and_proof_use_real_demo_views():
    html = INDEX.read_text(encoding="utf-8")
    assert "Switch between six current product views" in html
    assert 'data-view="write"' in html
    assert 'href="demo/?view=report"' in html
    assert 'href="demo/?view=conversation"' in html
    assert 'href="demo/?view=workshop"' in html
    for image in (
        "demo-report.png",
        "demo-conversation.png",
        "demo-write.png",
        "demo-workshop.png",
    ):
        assert f'src="assets/images/{image}"' in html
    assert "Choose the stance you need." in html
    assert "globally active" in html


def test_landing_describes_only_supported_archive_paths():
    html = _flat(INDEX.read_text(encoding="utf-8"))
    assert "Bring the archive you already have." in html
    assert "Douban diary Excel export" in html
    assert "CLI importer" in html
    assert "Write page" in html
    for unsupported in ("Day One", "Notion", "Google Docs", "generic Excel"):
        assert unsupported not in html


def test_landing_discloses_each_generation_payload_and_context_limit():
    html = _flat(INDEX.read_text(encoding="utf-8"))
    required = (
        "Entry Reflection sends the target entry, entries earlier in canonical chronology, and the active prompt. It never sends later entries.",
        "Life Report sends the full Journal Archive and the active prompt.",
        "General Conversation sends the full Journal Archive, the active prompt, the complete current-session history, and the current message.",
        "Entry Conversation sends the selected entry, its latest reflection when present, the complete thread history, the active prompt, and the current message.",
        "Prompt Workshop preview sends the target entry, entries earlier in canonical chronology, the draft instructions, and the selected model without persisting the output. It never sends later entries.",
        "first message of a new general Conversation",
        "separate model",
        "date title remains",
        "selected model's context window",
        "fails clearly instead of silently dropping",
    )
    for phrase in required:
        assert phrase in html


def test_landing_states_product_boundary_and_license_accurately():
    html = _flat(INDEX.read_text(encoding="utf-8"))
    assert "Unflincher is not therapy" in html
    assert "does not diagnose or treat" in html
    assert "does not impersonate a licensed professional" in html
    assert "does not replace professional care or relationships with other people" in html
    assert "Source available for noncommercial use under PolyForm Noncommercial 1.0.0." in html
    assert "self-hosted AI journal" not in html
    assert re.search(r"\bpersona\b", html, re.IGNORECASE) is None
    assert "commentary" not in html.lower()


def test_landing_loads_the_reveal_script():
    html = INDEX.read_text(encoding="utf-8")
    assert 'src="assets/js/site.js"' in html


def test_landing_is_responsive_and_reduced_motion_aware():
    css = LANDING_CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 768px)" in css
    assert "grid-template-columns: 1fr" in css
    assert "min-width: 0" in css
    assert "calc(100dvh - 4.75rem)" in css
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
