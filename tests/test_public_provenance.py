import importlib.util
import json
import subprocess
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
    # Capture-provenance and approved-history fields default to empty when undeclared.
    assert entry["approved_historical_sha256"] == []
    assert entry["fixture_sha256"] == ""
    assert entry["capture_command"] == ""
    assert entry["runtime"] == ""
    assert entry["source_ref"] == ""


def test_build_manifest_carries_through_declared_capture_metadata_and_history(tmp_path):
    _write_png(tmp_path / "demo-timeline.png", b"alpha")
    old_digest = "a" * 64
    sources = {
        "demo-timeline.png": {
            "origin": "synthetic-static-demo",
            "approved_historical_sha256": [old_digest, old_digest],
            "fixture_sha256": "b" * 64,
            "capture_command": "playwright capture",
            "runtime": "HeadlessChrome/151.0.0.0",
            "source_ref": "deadbeef",
        }
    }
    entry = provenance.build_manifest(tmp_path, sources)[0]
    # Duplicates in the declared allowlist are deduplicated and sorted for determinism.
    assert entry["approved_historical_sha256"] == [old_digest]
    assert entry["fixture_sha256"] == "b" * 64
    assert entry["capture_command"] == "playwright capture"
    assert entry["runtime"] == "HeadlessChrome/151.0.0.0"
    assert entry["source_ref"] == "deadbeef"


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


def test_verify_manifest_rejects_invalid_approved_historical_sha256_values(tmp_path):
    _write_png(tmp_path / "demo-timeline.png", b"alpha")
    base_entry = {
        "file": "demo-timeline.png",
        "sha256": provenance._sha256(tmp_path / "demo-timeline.png"),
        "bytes": (tmp_path / "demo-timeline.png").stat().st_size,
        "origin": "synthetic-static-demo",
        "view": "timeline",
        "viewport": "1440x900",
        "source": "site/demo/index.html",
        "fixture": "",
        "fixture_sha256": "",
        "capture_command": "",
        "runtime": "",
        "source_ref": "",
    }

    not_a_list = dict(base_entry, approved_historical_sha256="not-a-list")
    assert any(
        "invalid approved_historical_sha256 metadata" in error
        for error in provenance.verify_manifest(tmp_path, [not_a_list])
    )

    bad_hex = dict(base_entry, approved_historical_sha256=["not-hex"])
    assert any(
        "invalid approved historical sha256 value" in error
        for error in provenance.verify_manifest(tmp_path, [bad_hex])
    )

    duplicated = dict(
        base_entry, approved_historical_sha256=["a" * 64, "a" * 64]
    )
    assert any(
        "duplicate approved_historical_sha256" in error
        for error in provenance.verify_manifest(tmp_path, [duplicated])
    )

    self_referential = dict(
        base_entry, approved_historical_sha256=[base_entry["sha256"]]
    )
    assert any(
        "must not duplicate its own approved_historical_sha256" in error
        for error in provenance.verify_manifest(tmp_path, [self_referential])
    )


def test_verify_manifest_accepts_valid_approved_historical_sha256(tmp_path):
    _write_png(tmp_path / "demo-timeline.png", b"alpha")
    entry = {
        "file": "demo-timeline.png",
        "sha256": provenance._sha256(tmp_path / "demo-timeline.png"),
        "bytes": (tmp_path / "demo-timeline.png").stat().st_size,
        "origin": "synthetic-static-demo",
        "view": "timeline",
        "viewport": "1440x900",
        "source": "site/demo/index.html",
        "fixture": "",
        "fixture_sha256": "",
        "capture_command": "",
        "runtime": "",
        "source_ref": "",
        "approved_historical_sha256": ["b" * 64],
    }
    assert provenance.verify_manifest(tmp_path, [entry]) == []


def test_verify_manifest_rejects_non_commit_source_ref(tmp_path):
    _write_png(tmp_path / "demo-timeline.png", b"alpha")
    entry = provenance.build_manifest(
        tmp_path,
        {
            "demo-timeline.png": {
                "origin": "synthetic-static-demo",
                "source_ref": "dirty worktree with uncommitted changes",
            }
        },
    )[0]

    assert any(
        "source_ref must be a full Git commit SHA" in error
        for error in provenance.verify_manifest(tmp_path, [entry])
    )


def test_verify_manifest_rejects_source_changed_since_capture_commit(tmp_path):
    root = tmp_path / "repo"
    images_dir = root / "site" / "assets" / "images"
    source_path = root / "site" / "demo" / "index.html"
    images_dir.mkdir(parents=True)
    source_path.parent.mkdir(parents=True)
    _write_png(images_dir / "demo-timeline.png", b"alpha")
    source_path.write_text("first source\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "capture source",
        ],
        cwd=root,
        check=True,
    )
    source_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    entry = provenance.build_manifest(
        images_dir,
        {
            "demo-timeline.png": {
                "origin": "synthetic-static-demo",
                "source": "site/demo/index.html",
                "source_ref": source_ref,
            }
        },
    )[0]
    assert provenance.verify_manifest(images_dir, [entry]) == []

    source_path.write_text("changed source\n", encoding="utf-8")
    assert any(
        "source differs from source_ref for demo-timeline.png" in error
        for error in provenance.verify_manifest(images_dir, [entry])
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
        "demo-write.png",
        "demo-workshop.png",
    ):
        assert name in declared


def test_committed_demo_screenshots_declare_approved_history_and_capture_metadata():
    manifest = json.loads((IMAGES / "provenance.json").read_text(encoding="utf-8"))
    by_file = {entry["file"]: entry for entry in manifest}
    for name in (
        "demo-timeline.png",
        "demo-entry.png",
        "demo-report.png",
        "demo-conversation.png",
        "demo-write.png",
        "demo-workshop.png",
    ):
        entry = by_file[name]
        assert entry["approved_historical_sha256"], f"{name} must record prior approved digests"
        assert entry["sha256"] not in entry["approved_historical_sha256"]
        assert len(entry["fixture_sha256"]) == 64
        assert entry["capture_command"]
        assert entry["runtime"]
        assert entry["source_ref"]
        assert len(entry["source_ref"]) == 40
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{entry['source_ref']}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{name} source_ref is not a reachable Git commit"
