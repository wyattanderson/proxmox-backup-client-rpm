#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path


TARGET_MANIFESTS = [
    Path("proxmox-backup-client/Cargo.toml"),
    Path("pxar-bin/Cargo.toml"),
]
PREPARED_ROOT_PREFIX = "proxmox-backup-prepared"
PROXMOX_REGISTRY_SUBDIR = Path("vendor/proxmox-registry")
CRATES_IO_VENDOR_SUBDIR = Path("vendor/cargo")
METADATA_SUBDIR = Path(".rpm-metadata")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a fully offline source tarball for SRPM generation."
    )
    parser.add_argument("--dbgsym-deb", required=True, type=Path, help="Path to proxmox-backup-client dbgsym .deb")
    parser.add_argument("--checkout", required=True, type=Path, help="Path to proxmox-backup checkout")
    parser.add_argument("--downloads-dir", required=True, type=Path, help="Directory holding downloaded Proxmox .deb files")
    parser.add_argument("--work-dir", required=True, type=Path, help="Scratch directory for generated state")
    parser.add_argument("--output-dir", required=True, type=Path, help="Where to write source tarballs")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root for helper scripts",
    )
    return parser.parse_args()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def detect_version(checkout: Path) -> str:
    cargo_toml = (checkout / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r"(?ms)^\[workspace\.package\].*?^version = \"([^\"]+)\"", cargo_toml)
    if not match:
        raise SystemExit(f"unable to determine upstream version from {checkout / 'Cargo.toml'}")
    return match.group(1)


def detect_repoid(checkout: Path, version: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        repoid = result.stdout.strip()
        if repoid:
            return repoid
    except subprocess.CalledProcessError:
        pass
    return f"prepared-{version}"


def run_prepare_registry(
    repo_root: Path,
    dbgsym_deb: Path,
    checkout: Path,
    downloads_dir: Path,
    work_dir: Path,
) -> None:
    state_dir = work_dir / "state"
    registry_dir = work_dir / "registry"
    subprocess.run(
        [
            "python3",
            str(repo_root / "scripts" / "prepare-dbgsym-registry.py"),
            "--dbgsym-deb",
            str(dbgsym_deb),
            "--checkout",
            str(checkout),
            "--downloads-dir",
            str(downloads_dir),
            "--state-dir",
            str(state_dir),
            "--registry-dir",
            str(registry_dir),
            "--repo-root",
            str(repo_root),
        ],
        check=True,
    )


def create_upstream_tarball(checkout: Path, version: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"proxmox-backup-{version}.tar.gz"
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "archive",
            "--format=tar.gz",
            f"--prefix=proxmox-backup-{version}/",
            "HEAD",
            "-o",
            str(target),
        ],
        check=True,
    )
    return target


def extract_tarball(archive: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        top_level = sorted({member.name.split("/", 1)[0] for member in tar.getmembers() if member.name})
    subprocess.run(["tar", "-xzf", str(archive), "-C", str(destination)], check=True)
    if len(top_level) != 1:
        raise SystemExit(f"expected exactly one top-level directory in {archive}")
    return destination / top_level[0]


def patch_entries(source_tree: Path, absolute: bool = False) -> list[str]:
    patches: list[str] = []
    registry_dir = source_tree / PROXMOX_REGISTRY_SUBDIR
    for crate_dir in sorted(registry_dir.iterdir()):
        if not crate_dir.is_dir():
            continue
        name, _, version = crate_dir.name.rpartition("-")
        if not name or not version:
            continue
        path_value = str(crate_dir.resolve()) if absolute else crate_dir.relative_to(source_tree).as_posix()
        patches.append(f'"{name}" = {{ path = "{path_value}" }}')
    return patches


def write_cargo_config(source_tree: Path, repoid: str) -> None:
    config_dir = source_tree / ".cargo"
    config_dir.mkdir(parents=True, exist_ok=True)

    content = (
        "[patch.crates-io]\n"
        + "\n".join(patch_entries(source_tree, absolute=False))
        + "\n\n[source.crates-io]\n"
        + 'replace-with = "vendored-sources"\n\n'
        + "[source.vendored-sources]\n"
        + f'directory = "{CRATES_IO_VENDOR_SUBDIR.as_posix()}"\n'
        + "\n[env]\n"
        + f'REPOID = "{repoid}"\n'
    )
    (config_dir / "config.toml").write_text(content, encoding="utf-8")


def vendor_cargo_dependencies(source_tree: Path, vendor_dir: Path) -> None:
    cargo_home = vendor_dir.parent / "cargo-home"
    if cargo_home.exists():
        shutil.rmtree(cargo_home)
    vendor_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CARGO_HOME"] = str(cargo_home)
    temp_config_dir = cargo_home / "temp-config"
    temp_config_dir.mkdir(parents=True, exist_ok=True)
    (temp_config_dir / "config.toml").write_text(
        "[patch.crates-io]\n" + "\n".join(patch_entries(source_tree, absolute=True)) + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "cargo",
            "generate-lockfile",
            "--manifest-path",
            str(source_tree / "Cargo.toml"),
            "--config",
            str(temp_config_dir / "config.toml"),
        ],
        cwd=source_tree,
        env=env,
        check=True,
    )
    subprocess.run(
        [
            "cargo",
            "vendor",
            "--versioned-dirs",
            "--manifest-path",
            str(source_tree / "Cargo.toml"),
            "--config",
            str(temp_config_dir / "config.toml"),
            str(vendor_dir),
        ],
        cwd=source_tree,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def prepare_source_tree(
    upstream_tarball: Path,
    version: str,
    repoid: str,
    work_dir: Path,
    state_dir: Path,
    registry_dir: Path,
) -> Path:
    unpack_root = work_dir / "prepared-src"
    source_tree = extract_tarball(upstream_tarball, unpack_root)

    desired_name = f"{PREPARED_ROOT_PREFIX}-{version}"
    prepared_tree = source_tree.parent / desired_name
    if prepared_tree.exists():
        shutil.rmtree(prepared_tree)
    source_tree.rename(prepared_tree)

    project_config = prepared_tree / ".cargo" / "config.toml"
    if project_config.exists():
        project_config.unlink()

    staged_registry_dir = prepared_tree / PROXMOX_REGISTRY_SUBDIR
    if staged_registry_dir.exists():
        shutil.rmtree(staged_registry_dir)
    staged_registry_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(registry_dir, staged_registry_dir)

    metadata_dir = prepared_tree / METADATA_SUBDIR
    metadata_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "dbgsym-crates.json",
        "staged-direct.json",
        "target-transitive-crates.json",
        "target-final-crates.json",
        "staged.json",
        "summary.json",
    ):
        source = state_dir / name
        if source.exists():
            shutil.copy2(source, metadata_dir / name)

    vendor_cargo_dependencies(prepared_tree, prepared_tree / CRATES_IO_VENDOR_SUBDIR)
    write_cargo_config(prepared_tree, repoid)
    return prepared_tree


def create_prepared_tarball(prepared_tree: Path, version: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{PREPARED_ROOT_PREFIX}-{version}.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        tar.add(prepared_tree, arcname=prepared_tree.name)
    return target


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    checkout = args.checkout.resolve()
    work_dir = args.work_dir.resolve()
    output_dir = args.output_dir.resolve()
    downloads_dir = args.downloads_dir.resolve()
    dbgsym_deb = args.dbgsym_deb.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_prepare_registry(repo_root, dbgsym_deb, checkout, downloads_dir, work_dir)

    version = detect_version(checkout)
    repoid = detect_repoid(checkout, version)
    upstream_tarball = create_upstream_tarball(checkout, version, output_dir)
    prepared_tree = prepare_source_tree(
        upstream_tarball,
        version,
        repoid,
        work_dir,
        work_dir / "state",
        work_dir / "registry",
    )
    prepared_tarball = create_prepared_tarball(prepared_tree, version, output_dir)

    write_json(
        output_dir / "sources.json",
        {
            "version": version,
            "upstream_tarball": str(upstream_tarball),
            "prepared_tarball": str(prepared_tarball),
            "prepared_tree": str(prepared_tree),
            "repoid": repoid,
            "state_dir": str((work_dir / "state").resolve()),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
