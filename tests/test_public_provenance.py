import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "site" / "assets" / "images"


def _load_provenance():
    spec = importlib.util.spec_from_file_location(
        "public_provenance", ROOT / "tools" / "public_provenance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provenance = _load_provenance()


def _write_png(path: Path, payload: bytes) -> None:
    # Minimal but valid PNG signature plus payload; provenance only hashes bytes.
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + payload)


def test_build_manifest_records_sha256_and_metadata(tmp_path):
    _write_png(tmp_path / "demo-timeline.png", b"alpha")
    sources = {
        "demo-timeline.png": {
            "origin": "synthetic-static-demo",
            "view": "timeline",
            "viewport": "1440x900",
            "source": "site/demo/index.html",
            "fixture": "site/data/sample-journal.json",
        }
    }
    manifest = provenance.build_manifest(tmp_path, sources)
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry["file"] == "demo-timeline.png"
    assert entry["origin"] == "synthetic-static-demo"
    assert entry["source"] == "site/demo/index.html"
    assert len(entry["sha256"]) == 64
    assert entry["bytes"] > 0


def test_build_manifest_rejects_missing_declared_image(tmp_path):
    with pytest.raises(FileNotFoundError):
        provenance.build_manifest(tmp_path, {"demo-missing.png": {"origin": "synthetic-static-demo"}})


def test_verify_manifest_detects_tampering_and_undeclared_files(tmp_path):
    _write_png(tmp_path / "demo-timeline.png", b"alpha")
    sources = {"demo-timeline.png": {"origin": "synthetic-static-demo"}}
    manifest = provenance.build_manifest(tmp_path, sources)
    assert provenance.verify_manifest(tmp_path, manifest) == []

    _write_png(tmp_path / "demo-timeline.png", b"tampered")
    assert any("sha256 mismatch" in error for error in provenance.verify_manifest(tmp_path, manifest))

    _write_png(tmp_path / "demo-timeline.png", b"alpha")
    _write_png(tmp_path / "sneaky.png", b"beta")
    assert any("undeclared image" in error for error in provenance.verify_manifest(tmp_path, manifest))


def test_verify_manifest_rejects_non_synthetic_origin(tmp_path):
    _write_png(tmp_path / "demo-timeline.png", b"alpha")
    manifest = [{"file": "demo-timeline.png", "sha256": provenance._sha256(tmp_path / "demo-timeline.png"), "bytes": 5, "origin": "production-capture"}]
    assert any("non-synthetic origin" in error for error in provenance.verify_manifest(tmp_path, manifest))


def test_verify_manifest_requires_svg_assets_to_be_declared(tmp_path):
    (tmp_path / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    assert any(
        "undeclared image" in error
        for error in provenance.verify_manifest(tmp_path, [])
    )


def test_committed_provenance_matches_committed_images():
    manifest = json.loads((IMAGES / "provenance.json").read_text(encoding="utf-8"))
    sources = json.loads(
        (ROOT / "tools" / "public_image_sources.json").read_text(encoding="utf-8")
    )
    assert provenance.verify_manifest(IMAGES, manifest) == []
    assert manifest == provenance.build_manifest(IMAGES, sources)
    declared = {entry["file"] for entry in manifest}
    for name in (
        "favicon.svg",
        "demo-timeline.png",
        "demo-entry.png",
        "demo-report.png",
        "demo-conversation.png",
        "demo-workshop.png",
    ):
        assert name in declared
