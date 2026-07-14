"""Provenance contract for public image assets.

Pure stdlib. Every committed public image must be declared in tools/public_image_sources.json
with a synthetic origin, and site/assets/images/provenance.json records its SHA256 and semantic
source metadata. Reused by tests/test_public_provenance.py and the readiness audit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ALLOWED_ORIGINS = {
    "synthetic-static-demo",
    "synthetic-social-template",
    "synthetic-brand-asset",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest(images_dir: Path, sources: dict) -> list[dict]:
    manifest: list[dict] = []
    for name in sorted(sources):
        meta = sources[name]
        image_path = images_dir / name
        if not image_path.is_file():
            raise FileNotFoundError(f"declared image missing: {name}")
        manifest.append(
            {
                "file": name,
                "sha256": _sha256(image_path),
                "bytes": image_path.stat().st_size,
                "origin": meta["origin"],
                "view": meta.get("view", ""),
                "viewport": meta.get("viewport", ""),
                "source": meta.get("source", ""),
                "fixture": meta.get("fixture", ""),
            }
        )
    return manifest


def verify_manifest(images_dir: Path, manifest: list[dict]) -> list[str]:
    errors: list[str] = []
    declared: set[str] = set()
    for entry in manifest:
        name = entry.get("file")
        if not isinstance(name, str) or not name:
            errors.append("manifest entry has an invalid file name")
            continue
        declared.add(name)
        image_path = images_dir / name
        if not image_path.is_file():
            errors.append(f"missing image: {name}")
            continue
        if _sha256(image_path) != entry.get("sha256"):
            errors.append(f"sha256 mismatch for {name}")
        if image_path.stat().st_size != entry.get("bytes"):
            errors.append(f"byte-size mismatch for {name}")
        if entry.get("origin") not in ALLOWED_ORIGINS:
            errors.append(f"non-synthetic origin for {name}: {entry.get('origin')!r}")
        for field in ("view", "viewport", "source", "fixture"):
            if not isinstance(entry.get(field), str):
                errors.append(f"invalid {field} metadata for {name}")
    for pattern in ("*.png", "*.svg"):
        for image_path in sorted(images_dir.glob(pattern)):
            if image_path.name not in declared:
                errors.append(f"undeclared image on disk: {image_path.name}")
    return errors


def main(argv=None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parents[1]
    images_dir = root / "site" / "assets" / "images"
    sources = json.loads((root / "tools" / "public_image_sources.json").read_text(encoding="utf-8"))
    manifest_path = images_dir / "provenance.json"
    command = argv[0] if argv else "verify"

    if command == "build":
        manifest = build_manifest(images_dir, sources)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {manifest_path} with {len(manifest)} entries")
        return 0
    if command == "verify":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        errors = verify_manifest(images_dir, manifest)
        try:
            expected_manifest = build_manifest(images_dir, sources)
        except FileNotFoundError as error:
            errors.append(str(error))
        else:
            if manifest != expected_manifest:
                errors.append("manifest does not match declared image sources")
        for error in errors:
            print(error)
        return 1 if errors else 0
    print(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
