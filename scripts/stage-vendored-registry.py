#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage an offline Proxmox crate registry from a vendored lock manifest."
    )
    parser.add_argument("--lock-manifest", required=True, type=Path, help="Path to staged.json from source prep")
    parser.add_argument("--vendor-dir", required=True, type=Path, help="Directory containing vendored .deb files")
    parser.add_argument("--registry-dir", required=True, type=Path, help="Output directory for extracted crates")
    parser.add_argument("--manifest-out", required=True, type=Path, help="Write final staging summary here")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root containing scripts/ and patches/",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_extract_script(repo_root: Path, deb_path: Path, crate_name: str, crate_version: str, registry_dir: Path) -> None:
    script = repo_root / "scripts" / "extract-deb-crate.sh"
    for existing in registry_dir.glob(f"{crate_name}-*"):
        if existing.is_dir():
            shutil.rmtree(existing)
    subprocess.run([str(script), str(deb_path), crate_name, crate_version, str(registry_dir)], check=True)


def apply_patch(crate_dir: Path, patch_file: Path) -> bool:
    dry_run = subprocess.run(
        ["patch", "--dry-run", "-p1", "-i", str(patch_file)],
        cwd=crate_dir,
        capture_output=True,
        text=True,
    )
    if dry_run.returncode == 0:
        subprocess.run(["patch", "-p1", "-i", str(patch_file)], cwd=crate_dir, check=True)
        return True

    reverse = subprocess.run(
        ["patch", "--dry-run", "-R", "-p1", "-i", str(patch_file)],
        cwd=crate_dir,
        capture_output=True,
        text=True,
    )
    if reverse.returncode == 0:
        return False

    raise RuntimeError(
        f"failed to apply patch {patch_file} in {crate_dir}\nstdout:\n{dry_run.stdout}\nstderr:\n{dry_run.stderr}"
    )


def apply_local_patches(repo_root: Path, registry_dir: Path) -> list[dict[str, str]]:
    patches_root = repo_root / "patches"
    applied: list[dict[str, str]] = []
    if not patches_root.is_dir():
        return applied

    for patch_dir in sorted(patches_root.iterdir()):
        if not patch_dir.is_dir():
            continue
        for crate_dir in sorted(registry_dir.glob(f"{patch_dir.name}-*")):
            for patch_file in sorted(patch_dir.glob("*.patch")):
                if apply_patch(crate_dir, patch_file):
                    applied.append(
                        {
                            "crate_dir": crate_dir.name,
                            "patch": str(patch_file.relative_to(repo_root)),
                        }
                    )
    return applied


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    vendor_dir = args.vendor_dir.resolve()
    registry_dir = args.registry_dir.resolve()
    payload = read_json(args.lock_manifest.resolve())

    registry_dir.mkdir(parents=True, exist_ok=True)

    staged: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for entry in payload.get("staged", []):
        crate_name = str(entry["crate"])
        resolved_version = str(entry["resolved_version"])
        deb_name = Path(str(entry["deb_path"])).name
        deb_path = vendor_dir / deb_name

        if not deb_path.exists():
            missing.append(
                {
                    "crate": crate_name,
                    "resolved_version": resolved_version,
                    "deb_name": deb_name,
                }
            )
            continue

        run_extract_script(repo_root, deb_path, crate_name, resolved_version, registry_dir)
        staged.append(
            {
                "crate": crate_name,
                "resolved_version": resolved_version,
                "deb_name": deb_name,
                "deb_path": str(deb_path),
            }
        )

    applied_patches = apply_local_patches(repo_root, registry_dir)
    result = {
        "lock_manifest": str(args.lock_manifest.resolve()),
        "vendor_dir": str(vendor_dir),
        "registry_dir": str(registry_dir),
        "staged_count": len(staged),
        "missing_count": len(missing),
        "staged": staged,
        "missing": missing,
        "applied_patches": applied_patches,
    }
    write_json(args.manifest_out.resolve(), result)
    if missing:
        raise SystemExit(f"missing {len(missing)} vendored .deb files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
