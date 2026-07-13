import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "unflincher" / "static"

EXPECTED_SHA256 = {
    "fonts/IBMPlexSansCondensed-Regular.woff2": "490b0b8a1f738857f90063ed9ff5d9c29a4dde2139db83ed80252e2a676f3226",
    "fonts/IBMPlexSansCondensed-Medium.woff2": "d325f988f58729a26cc062c06738d6e234ed1d3de685dcb914d86aec6b36ce94",
    "fonts/IBMPlexSansCondensed-SemiBold.woff2": "ec28b4f7f62878e81f86936dee2c49dd688bf0a654469e088d5cb758ee1fbbff",
    "fonts/IBMPlexSansCondensed-Bold.woff2": "7ec435d503813d866db75da8fcc8fb4b1c1ce853475578e7a409c50bf9ab0516",
    "fonts/IBMPlexMono-Regular.woff2": "49ce58b41a0e1cb921c0f58d9a5b8b96a2cc21437c7066f3ba4f24873076d131",
    "fonts/IBMPlexMono-Medium.woff2": "8c2c290cbd998fa1f647e4572aca6ebbd72589551b0f3f9f8bb8628fbb8219d5",
    "fonts/IBMPlexMono-SemiBold.woff2": "ed5eaca7522336959d6c3810bd9bb78424f0d964082d581bfbea169ee08d14e3",
    "fonts/OFL.txt": "7e6b2818edbd8f6a01ae80641cc8f16a51080d08fb4e532be3a0b6f74adb07da",
}


def test_pinned_ibm_plex_assets_match_expected_hashes():
    for relative_path, expected in EXPECTED_SHA256.items():
        content = (STATIC / relative_path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected


def test_balanced_graphite_stylesheets_use_approved_tokens_and_system_font():
    for name in ("tokens.css", "base.css", "shell.css", "components.css", "pages.css"):
        assert (STATIC / "css" / name).is_file()

    tokens = (STATIC / "css" / "tokens.css").read_text()
    for declaration in (
        "--bg: #1d1e1d",
        "--chrome: #222322",
        "--entry-plane: #202220",
        "--commentary-plane: #232523",
        "--text: #e0ddd6",
        "--prose: #c7c2ba",
        "--muted: #85827b",
        "--rule: #444744",
        "--soft-surface: #2c2e2c",
    ):
        assert declaration in tokens

    css = "\n".join(
        path.read_text() for path in sorted((STATIC / "css").glob("*.css"))
    )
    assert "@font-face" not in css
    assert ".woff2" not in css
    assert "IBM Plex" not in css
    assert "tabular-nums" in css


def test_balanced_graphite_components_are_flat_and_stateful():
    css = (STATIC / "css" / "components.css").read_text()
    for selector in (
        ".page-heading",
        ".button",
        ".status-mark",
        ".empty-state",
        ".confirmation-dialog",
        ".conversation-message",
        ".session-row",
    ):
        assert selector in css
    assert "var(--soft-surface)" in css
    assert "var(--rule)" in css
    assert "box-shadow" not in css
    assert "linear-gradient" not in css
