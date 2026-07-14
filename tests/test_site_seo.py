import json
import re
from pathlib import Path
from xml.dom import minidom

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
BASE = "https://sinmentis.github.io/unflincher/"


def _json_ld(html: str) -> dict:
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    assert match, "JSON-LD block missing"
    return json.loads(match.group(1))


def test_index_has_canonical_and_social_metadata():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{BASE}">' in html
    assert f'content="{BASE}assets/images/social-preview.png"' in html
    assert '<meta property="og:type" content="website">' in html
    assert '<meta property="og:image:width" content="1280">' in html
    assert '<meta property="og:image:height" content="640">' in html
    assert '<meta property="og:image:alt"' in html
    assert '<meta name="twitter:card" content="summary_large_image">' in html
    assert '<meta name="twitter:image:alt"' in html


def test_index_json_ld_is_accurate_and_makes_no_offer():
    html = (SITE / "index.html").read_text(encoding="utf-8")
    data = _json_ld(html)
    graph = data["@graph"]
    types = {node["@type"] for node in graph}
    assert {"WebSite", "SoftwareApplication", "SoftwareSourceCode"}.issubset(types)
    blob = json.dumps(data)
    assert "polyformproject.org/licenses/noncommercial/1.0.0" in blob
    assert '"price"' not in blob
    assert '"offers"' not in blob
    assert "open source" not in blob.lower()


def test_demo_page_has_its_own_canonical():
    html = (SITE / "demo" / "index.html").read_text(encoding="utf-8")
    assert f'<link rel="canonical" href="{BASE}demo/">' in html
    assert '<meta name="twitter:title" content="Unflincher interactive demo">' in html
    assert '<meta name="twitter:description"' in html


def test_public_robots_allows_crawling_and_points_to_sitemap():
    robots = (SITE / "robots.txt").read_text(encoding="utf-8")
    assert "Allow: /" in robots
    assert f"Sitemap: {BASE}sitemap.xml" in robots


def test_sitemap_lists_only_public_pages():
    doc = minidom.parse(str(SITE / "sitemap.xml"))
    locs = {node.firstChild.data for node in doc.getElementsByTagName("loc")}
    assert locs == {BASE, f"{BASE}demo/"}


def test_social_preview_is_declared_synthetic():
    manifest = json.loads((SITE / "assets" / "images" / "provenance.json").read_text(encoding="utf-8"))
    social = [entry for entry in manifest if entry["file"] == "social-preview.png"]
    assert social and social[0]["origin"] == "synthetic-social-template"
    assert social[0]["source"] == "tools/public-assets/social-preview.html"


def test_social_preview_source_is_not_in_the_published_site_tree():
    assert (ROOT / "tools" / "public-assets" / "social-preview.html").is_file()
    assert not (SITE / "assets" / "images" / "social-preview.html").exists()
