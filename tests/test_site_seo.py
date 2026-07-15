import json
import re
from html import unescape
from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
BASE = "https://sinmentis.github.io/unflincher/"
LLMS = SITE / "llms.txt"
SOCIAL_ALT = "Dark social card with the text AI reflection partner, the promise An outside perspective on years of your journal, and the Unflincher repository URL."
EXPECTED_FEATURES = (
    "Cross-year pattern reflection across a Journal Archive",
    "Entry Reflections grounded in journal context",
    "Life Reports with dated Entry References",
    "Follow-up Conversations about generated interpretations",
    "Companion, Coach, Challenger, Analyst, and Custom Perspectives",
    "Prompt Workshop previews without persistence",
    "Douban diary Excel import and manual writing",
    "Single-user self-hosting with local SQLite storage",
)
EXPECTED_FAQ = (
    (
        "What is Unflincher?",
        "Unflincher is an evidence-grounded AI reflection partner for people with years of journal entries. It reads across your Journal Archive, finds recurring patterns, points back to dated entries, and lets you challenge the interpretation in Conversation.",
    ),
    (
        "What is a Perspective?",
        "A Perspective is the globally active stance used for future Entry Reflections, Life Reports, and Conversations. Choose Companion, Coach, Challenger, Analyst, or Custom in Prompt Workshop. Changing it does not rewrite existing generated content.",
    ),
    (
        "What is a Life Report?",
        "A Life Report is a cross-year synthesis of recurring patterns, changes, contradictions, and goals. It names dated Entry References so you can compare the interpretation with the original writing.",
    ),
    (
        "What archive can I import?",
        "The supported archive import is an untouched Douban diary Excel export from the Tofu Chrome extension, handled by the CLI importer. You can also write entries directly in Unflincher. There is no browser upload or generic spreadsheet importer.",
    ),
    (
        "What leaves my host during generation?",
        "Entries, prompts, generated output, and Conversation history stay in local SQLite. GitHub Copilot is the only model integration. Depending on the feature, it receives the full Journal Archive or selected entry context, active or draft instructions, applicable Conversation history, and the current message. A separate model may receive the first general Conversation message once to generate a short title. The complete request must fit the selected model's context window, and Unflincher fails clearly instead of silently dropping older content.",
    ),
    (
        "How is Unflincher licensed?",
        "Unflincher is source available for noncommercial use under PolyForm Noncommercial 1.0.0. Commercial use requires a separate license.",
    ),
    (
        "Is Unflincher a substitute for therapy?",
        "Unflincher is not therapy, does not diagnose or treat, and does not impersonate a licensed professional. It does not replace professional care or relationships with other people.",
    ),
)


def _json_ld(html: str) -> dict:
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    assert match, "JSON-LD block missing"
    return json.loads(match.group(1))


def _graph_node(data: dict, node_type: str) -> dict:
    return next(node for node in data["@graph"] if node["@type"] == node_type)


def _plain(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _visible_faq(html: str) -> tuple[tuple[str, str], ...]:
    section = re.search(
        r'<section id="faq".*?</section>',
        html,
        re.DOTALL,
    )
    assert section, "visible FAQ section missing"
    pairs = re.findall(
        r"<details>\s*<summary>(.*?)</summary>\s*<p>(.*?)</p>\s*</details>",
        section.group(0),
        re.DOTALL,
    )
    return tuple((_plain(question), _plain(answer)) for question, answer in pairs)


def test_index_has_canonical_and_social_metadata():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    assert "<title>Unflincher: an AI reflection partner for your journal</title>" in html
    assert (
        '<meta name="description" content="Unflincher reads years of journal entries, '
        "finds recurring patterns, is designed to point back to dated entries, and keeps "
        'the interpretation open to conversation.">'
    ) in html
    assert f'<link rel="canonical" href="{BASE}">' in html
    assert f'content="{BASE}assets/images/social-preview.png"' in html
    assert '<meta property="og:type" content="website">' in html
    assert '<meta property="og:image:width" content="1280">' in html
    assert '<meta property="og:image:height" content="640">' in html
    assert f'<meta property="og:image:alt" content="{SOCIAL_ALT}">' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert f'<meta name="twitter:image:alt" content="{SOCIAL_ALT}">' in html


def test_index_json_ld_is_accurate_and_makes_no_offer():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    data = _json_ld(html)
    graph = data["@graph"]
    types = {node["@type"] for node in graph}
    assert {
        "WebSite",
        "SoftwareApplication",
        "SoftwareSourceCode",
        "FAQPage",
    }.issubset(types)
    blob = json.dumps(data)
    assert "polyformproject.org/licenses/noncommercial/1.0.0" in blob
    assert '"price"' not in blob
    assert '"offers"' not in blob
    assert '"aggregateRating"' not in blob
    assert '"review"' not in blob
    assert '"Organization"' not in blob
    assert '"MedicalWebPage"' not in blob
    assert "open source" not in blob.lower()


def test_index_feature_list_is_factual_and_complete():
    data = _json_ld((SITE / "index.html").read_text(encoding="utf-8"))
    software = _graph_node(data, "SoftwareApplication")
    assert tuple(software["featureList"]) == EXPECTED_FEATURES


def test_faq_json_ld_is_text_equivalent_to_visible_faq():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    visible = _visible_faq(html)
    faq = _graph_node(_json_ld(html), "FAQPage")
    structured = tuple(
        (entity["name"], entity["acceptedAnswer"]["text"])
        for entity in faq["mainEntity"]
        if entity["@type"] == "Question"
        and entity["acceptedAnswer"]["@type"] == "Answer"
    )
    assert visible == EXPECTED_FAQ
    assert structured == EXPECTED_FAQ


def test_demo_page_has_its_own_canonical():
    html = (SITE / "demo" / "index.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{BASE}demo/">' in html
    assert '<meta name="twitter:title" content="Unflincher interactive demo">' in html
    assert '<meta name="twitter:description"' in html
    assert f'<meta property="og:image:alt" content="{SOCIAL_ALT}">' in html
    assert f'<meta name="twitter:image:alt" content="{SOCIAL_ALT}">' in html


def test_public_robots_allows_crawling_and_points_to_sitemap():
    robots = (SITE / "robots.txt").read_text(encoding="utf-8")
    assert robots == (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {BASE}sitemap.xml\n"
    )


def test_sitemap_lists_only_public_pages():
    doc = minidom.parse(str(SITE / "sitemap.xml"))
    locs = {node.firstChild.data for node in doc.getElementsByTagName("loc")}
    assert locs == {BASE, f"{BASE}demo/"}


def test_sitemap_has_truthful_lastmod_for_existing_pages():
    doc = minidom.parse(str(SITE / "sitemap.xml"))
    pages = {}
    for url in doc.getElementsByTagName("url"):
        loc = url.getElementsByTagName("loc")[0].firstChild.data
        lastmods = url.getElementsByTagName("lastmod")
        assert len(lastmods) == 1
        pages[loc] = lastmods[0].firstChild.data
    assert pages == {
        BASE: "2026-07-16",
        f"{BASE}demo/": "2026-07-16",
    }


def test_llms_txt_matches_canonical_public_facts():
    text = " ".join(LLMS.read_text(encoding="utf-8").split())
    required = (
        "Unflincher is an evidence-grounded AI reflection partner for people with years of journal entries.",
        "Companion, Coach, Challenger, Analyst, and Custom",
        "Entry Reflection sends the full Journal Archive, the active prompt, and the selected-entry focus.",
        "Life Report sends the full Journal Archive and the active prompt.",
        "General Conversation sends the full Journal Archive, the active prompt, the complete current-session history, and the current message.",
        "Entry Conversation sends the selected entry, its latest reflection when present, the complete thread history, the active prompt, and the current message.",
        "Prompt Workshop preview sends the full Journal Archive, the selected-entry focus, the draft instructions, and the selected model without persisting the output.",
        "first message of a new general Conversation",
        "selected model's context window",
        "fails clearly instead of silently dropping",
        "untouched Douban diary Excel export from the Tofu Chrome extension",
        "There is no browser upload or generic spreadsheet importer.",
        "Source available for noncommercial use under PolyForm Noncommercial 1.0.0.",
        "The public demo contains only fictional data and performs no model calls, tracking, cookies, storage, or writable operations.",
        "Unflincher is not therapy, does not diagnose or treat",
    )
    for phrase in required:
        assert phrase in text
    assert "open source" not in text.lower()


def test_llms_txt_is_a_discovery_file_not_an_indexed_page():
    sitemap = (SITE / "sitemap.xml").read_text(encoding="utf-8")
    assert f"{BASE}llms.txt" not in sitemap
    assert not (SITE / "llms-full.txt").exists()


def test_social_preview_is_declared_synthetic():
    manifest = json.loads((SITE / "assets" / "images" / "provenance.json").read_text(encoding="utf-8"))
    social = [entry for entry in manifest if entry["file"] == "social-preview.png"]
    assert social and social[0]["origin"] == "synthetic-social-template"
    assert social[0]["source"] == "tools/public-assets/social-preview.html"


def test_social_preview_source_is_not_in_the_published_site_tree():
    assert (ROOT / "tools" / "public-assets" / "social-preview.html").is_file()
    assert not (SITE / "assets" / "images" / "social-preview.html").exists()
