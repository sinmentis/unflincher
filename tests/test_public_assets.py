import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "site" / "assets" / "fonts"
CSS = ROOT / "site" / "assets" / "css" / "site.css"

EXPECTED_SHA256 = {
    "IBMPlexSansCondensed-Regular.woff2": "490b0b8a1f738857f90063ed9ff5d9c29a4dde2139db83ed80252e2a676f3226",
    "IBMPlexSansCondensed-SemiBold.woff2": "ec28b4f7f62878e81f86936dee2c49dd688bf0a654469e088d5cb758ee1fbbff",
    "IBMPlexMono-Regular.woff2": "49ce58b41a0e1cb921c0f58d9a5b8b96a2cc21437c7066f3ba4f24873076d131",
    "Newsreader-Regular.woff2": "6e4f2958c3a7c4a80acde4e5a679abe7e01bc1e30b92be3c7a8b696ef401d101",
}


def test_public_fonts_match_pinned_hashes():
    for name, expected in EXPECTED_SHA256.items():
        content = (FONTS / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected


def test_public_fonts_ship_license_notices():
    for name in ("OFL-IBMPlex.txt", "OFL-Newsreader.txt"):
        text = (FONTS / name).read_text(encoding="utf-8", errors="ignore")
        assert "SIL Open Font License" in text
    # The vendored OFL notices are stored byte-for-byte (upstream IBM Plex uses
    # CRLF and both texts carry a canonical trailing space), so Git's whitespace
    # gate must be disabled for them or `git diff --check` fails. Assert the exact
    # rule that exempts vendored *.txt from both EOL normalization and whitespace.
    rules = (FONTS / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "*.txt -text -whitespace" in rules


def test_site_css_uses_midnight_manuscript_tokens():
    css = CSS.read_text(encoding="utf-8")
    for declaration in (
        "--night: #0e100f",
        "--graphite: #171a18",
        "--graphite-raised: #1d201d",
        "--ink: #e8e4dc",
        "--muted: #9e9a92",
        "--line: #41433e",
        "--accent: #c8ad78",
    ):
        assert declaration in css
    assert css.count("--accent: #c8ad78") == 1
    assert "@font-face" in css
    assert "Newsreader-Regular.woff2" in css
    assert "blur(" not in css
    assert "box-shadow" not in css


def test_public_favicon_matches_the_current_teal_product_mark():
    svg = (ROOT / "site" / "assets" / "images" / "favicon.svg").read_text(encoding="utf-8")
    assert "#a4ded1" in svg
    assert "#171815" in svg
    assert ">U<" in svg
