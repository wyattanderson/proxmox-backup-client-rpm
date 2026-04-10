#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Iterable


DEFAULT_PACKAGE_INDEXES = [
    "http://download.proxmox.com/debian/devel/dists/trixie/main/binary-amd64/Packages.gz",
]
PROXMOX_PREFIXES = ("proxmox-", "pbs-")
SPECIAL_CRATES = {"pxar", "pathpatterns"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage Proxmox crates recovered from dbgsym DWARF into a local registry tree."
    )
    parser.add_argument("--crate-list", required=True, type=Path)
    parser.add_argument("--registry-dir", required=True, type=Path)
    parser.add_argument("--downloads-dir", required=True, type=Path)
    parser.add_argument("--manifest-out", required=True, type=Path)
    parser.add_argument(
        "--packages-index",
        dest="packages_indexes",
        action="append",
        default=[],
        help="Append a Packages.gz URL to search. Defaults to Proxmox trixie devel.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    return parser.parse_args()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def is_proxmox_crate(crate_name: str) -> bool:
    return crate_name == "pxar" or crate_name in SPECIAL_CRATES or crate_name.startswith(PROXMOX_PREFIXES)


def parse_packages_index(raw_text: str) -> list[dict[str, str]]:
    packages: list[dict[str, str]] = []
    for chunk in raw_text.split("\n\n"):
        fields: dict[str, str] = {}
        current_key: str | None = None
        for line in chunk.splitlines():
            if not line.strip():
                continue
            if line.startswith(" ") and current_key:
                fields[current_key] += "\n" + line[1:]
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            current_key = key
            fields[key] = value.strip()
        if fields:
            packages.append(fields)
    return packages


def fetch_packages(packages_url: str) -> list[dict[str, str]]:
    with urllib.request.urlopen(packages_url) as response:
        compressed = response.read()
    raw = gzip.decompress(compressed).decode("utf-8")
    return parse_packages_index(raw)


def load_candidate_packages(indexes: Iterable[str]) -> dict[tuple[str, str], dict[str, str]]:
    candidates: dict[tuple[str, str], dict[str, str]] = {}
    for index_url in indexes:
        base_url = index_url.rsplit("/", 1)[0] + "/"
        for pkg in fetch_packages(index_url):
            package_name = pkg.get("Package", "")
            filename = pkg.get("Filename", "")
            version = pkg.get("Version", "")
            if not package_name.startswith(("rust-", "librust-")) or not filename.endswith(".deb") or not version:
                continue
            source = pkg.get("Source", "")
            candidate = {
                "package": package_name,
                "source": source,
                "version": version,
                "filename": filename,
                "url": base_url + filename.rsplit("/", 1)[-1],
            }
            candidates[(package_name, version)] = candidate
    return candidates


def deb_base_version(version: str) -> str:
    version = version.split(":", 1)[-1]
    return version.rsplit("-", 1)[0]


def parse_semver(version: str) -> tuple[int, int, int, str | None]:
    core, sep, suffix = version.partition("-")
    parts = core.split(".")
    while len(parts) < 3:
        parts.append("0")
    major, minor, patch = (int(parts[0]), int(parts[1]), int(parts[2]))
    return major, minor, patch, suffix or None


def compare_semver(left: str, right: str) -> int:
    left_major, left_minor, left_patch, left_suffix = parse_semver(left)
    right_major, right_minor, right_patch, right_suffix = parse_semver(right)
    left_key = (left_major, left_minor, left_patch)
    right_key = (right_major, right_minor, right_patch)
    if left_key != right_key:
        return -1 if left_key < right_key else 1
    if left_suffix == right_suffix:
        return 0
    if left_suffix is None:
        return 1
    if right_suffix is None:
        return -1
    return -1 if left_suffix < right_suffix else 1


def requirement_base(requirement: str) -> str:
    requirement = requirement.strip()
    for prefix in ("^", "="):
        if requirement.startswith(prefix):
            return requirement[len(prefix) :].strip()
    return requirement


def semver_satisfies(requirement: str, version: str) -> bool:
    requirement = requirement.strip()
    if not requirement:
        return False

    if requirement.startswith("="):
        return compare_semver(version, requirement_base(requirement)) == 0

    base = requirement_base(requirement)
    major, minor, patch, _ = parse_semver(base)
    version_major, version_minor, version_patch, _ = parse_semver(version)
    if compare_semver(version, f"{major}.{minor}.{patch}") < 0:
        return False

    if major > 0:
        return version_major == major
    if minor > 0:
        return version_major == 0 and version_minor == minor
    return version_major == 0 and version_minor == 0 and version_patch == patch


def is_exact_requirement(requirement: str | None) -> bool:
    return bool(requirement and requirement.strip().startswith("="))


def same_minor_series(left: str, right: str) -> bool:
    left_major, left_minor, _, _ = parse_semver(left)
    right_major, right_minor, _, _ = parse_semver(right)
    return (left_major, left_minor) == (right_major, right_minor)


def crate_to_package_names(crate_name: str) -> list[str]:
    deb_crate = crate_name.replace("_", "-")
    return [f"rust-{deb_crate}", f"librust-{deb_crate}-dev"]


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def run_extract_script(repo_root: Path, deb_path: Path, crate_name: str, crate_version: str, registry_dir: Path) -> None:
    script = repo_root / "scripts" / "extract-deb-crate.sh"
    for existing in registry_dir.glob(f"{crate_name}-*"):
        if existing.is_dir():
            shutil.rmtree(existing)
    subprocess.run(
        [str(script), str(deb_path), crate_name, crate_version, str(registry_dir)],
        check=True,
    )


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
                    applied.append({"crate_dir": crate_dir.name, "patch": str(patch_file.relative_to(repo_root))})
    return applied


def select_package(
    crate_name: str,
    crate_version: str,
    candidates: dict[tuple[str, str], dict[str, str]],
    requirement: str | None = None,
    observed_version: str | None = None,
) -> dict[str, str] | None:
    package_names = crate_to_package_names(crate_name)
    exact_matches: list[dict[str, str]] = []
    compatible_matches: list[dict[str, str]] = []
    observed_minor_matches: list[dict[str, str]] = []

    for package_name, package_version in sorted(candidates):
        if package_name not in package_names:
            continue
        candidate = candidates[(package_name, package_version)]
        upstream_version = deb_base_version(package_version)
        if is_exact_requirement(requirement) and upstream_version == crate_version:
            exact_matches.append(candidate)
            continue
        if requirement and semver_satisfies(requirement, upstream_version):
            compatible_matches.append(candidate)
            if observed_version and same_minor_series(observed_version, upstream_version):
                observed_minor_matches.append(candidate)
        elif not requirement and upstream_version == crate_version:
            exact_matches.append(candidate)

    if exact_matches:
        exact_matches.sort(key=lambda item: parse_semver(deb_base_version(item["version"])))
        return exact_matches[-1]

    if compatible_matches:
        if observed_minor_matches:
            observed_minor_matches.sort(key=lambda item: parse_semver(deb_base_version(item["version"])))
            return observed_minor_matches[-1]
        compatible_matches.sort(key=lambda item: parse_semver(deb_base_version(item["version"])))
        return compatible_matches[-1]

    return None


def main() -> int:
    args = parse_args()
    crate_payload = read_json(args.crate_list)
    requested = [
        crate
        for crate in crate_payload.get("crates", [])
        if is_proxmox_crate(str(crate.get("crate", "")))
    ]

    package_indexes = args.packages_indexes or DEFAULT_PACKAGE_INDEXES
    candidates = load_candidate_packages(package_indexes)

    args.registry_dir.mkdir(parents=True, exist_ok=True)
    args.downloads_dir.mkdir(parents=True, exist_ok=True)

    staged: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []

    for crate in requested:
        crate_name = str(crate["crate"])
        crate_version = str(crate["version"])
        requirement = crate.get("requirement")
        observed_version = crate.get("observed_version")
        package = select_package(
            crate_name,
            crate_version,
            candidates,
            str(requirement) if requirement else None,
            str(observed_version) if observed_version else None,
        )
        if not package:
            missing.append({"crate": crate_name, "version": crate_version})
            continue

        deb_name = Path(package["filename"]).name
        deb_path = args.downloads_dir / deb_name
        if not deb_path.exists():
            download(package["url"], deb_path)

        resolved_version = deb_base_version(package["version"])
        run_extract_script(args.repo_root, deb_path, crate_name, resolved_version, args.registry_dir)
        staged.append(
            {
                "crate": crate_name,
                "requested_version": crate_version,
                "version": crate_version,
                "resolved_version": resolved_version,
                "package": package["package"],
                "deb_version": package["version"],
                "deb_path": str(deb_path),
            }
        )

    applied_patches = apply_local_patches(args.repo_root, args.registry_dir)

    payload = {
        "crate_list": str(args.crate_list.resolve()),
        "registry_dir": str(args.registry_dir.resolve()),
        "downloads_dir": str(args.downloads_dir.resolve()),
        "package_indexes": package_indexes,
        "staged_count": len(staged),
        "missing_count": len(missing),
        "staged": staged,
        "missing": missing,
        "applied_patches": applied_patches,
    }
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
