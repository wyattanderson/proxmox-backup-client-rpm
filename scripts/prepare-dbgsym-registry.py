#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.error import URLError


DEFAULT_PACKAGE_INDEXES = [
    "http://download.proxmox.com/debian/devel/dists/trixie/main/binary-amd64/Packages.gz",
]
TARGET_MANIFESTS = [
    Path("proxmox-backup-client/Cargo.toml"),
    Path("pxar-bin/Cargo.toml"),
]
PROXMOX_PREFIXES = ("proxmox-", "pbs-")
SPECIAL_CRATES = {"pxar", "pathpatterns"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a local Proxmox crate registry using dbgsym DWARF and the "
            "Proxmox Debian archive, focused on proxmox-backup-client and pxar."
        )
    )
    parser.add_argument("--dbgsym-deb", required=True, type=Path, help="Path to proxmox-backup-client dbgsym .deb")
    parser.add_argument("--checkout", required=True, type=Path, help="Path to proxmox-backup checkout")
    parser.add_argument("--downloads-dir", required=True, type=Path, help="Directory for downloaded .deb artifacts")
    parser.add_argument("--state-dir", required=True, type=Path, help="Directory for generated JSON manifests")
    parser.add_argument("--registry-dir", required=True, type=Path, help="Directory for extracted crate sources")
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
        default=Path(__file__).resolve().parents[1],
        help="Repository root for locating helper scripts and patches",
    )
    return parser.parse_args()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def is_relevant(crate_name: str) -> bool:
    return crate_name == "pxar" or crate_name in SPECIAL_CRATES or crate_name.startswith(PROXMOX_PREFIXES)


def ar_members(deb_path: Path) -> list[str]:
    result = subprocess.run(
        ["ar", "t", str(deb_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def extract_debug_files(deb_path: Path, work_dir: Path) -> list[Path]:
    data_members = [member for member in ar_members(deb_path) if member.startswith("data.tar.")]
    if not data_members:
        raise SystemExit(f"no data.tar.* member found in {deb_path}")

    data_member = data_members[0]
    archive_path = work_dir / data_member
    extract_root = work_dir / "root"
    extract_root.mkdir(parents=True, exist_ok=True)

    with archive_path.open("wb") as archive_file:
        subprocess.run(["ar", "p", str(deb_path), data_member], check=True, stdout=archive_file)

    subprocess.run(["tar", "-xaf", str(archive_path), "-C", str(extract_root)], check=True)
    return sorted(extract_root.glob("usr/lib/debug/.build-id/*/*.debug"))


def readelf_decodedline(debug_file: Path) -> str:
    result = subprocess.run(
        ["readelf", "--debug-dump=decodedline", str(debug_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def rustc_comment(debug_file: Path) -> str | None:
    result = subprocess.run(
        ["readelf", "-p", ".comment", str(debug_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if "rustc version" in line:
            return line.split("]", 1)[-1].strip()
    return None


def extract_dbgsym_crates(deb_path: Path) -> dict:
    import re

    crate_path_re = re.compile(r"(/usr/share/cargo/registry/([^/ ]+)-([0-9][^/ ]*))")
    with tempfile.TemporaryDirectory(prefix="dbgsym-registry-") as temp_dir:
        debug_files = extract_debug_files(deb_path, Path(temp_dir))
        crates: dict[tuple[str, str], dict[str, str]] = {}
        rustc_versions: list[str] = []

        for debug_file in debug_files:
            comment = rustc_comment(debug_file)
            if comment and comment not in rustc_versions:
                rustc_versions.append(comment)

            for match in crate_path_re.finditer(readelf_decodedline(debug_file)):
                full_path, crate, version = match.groups()
                crates.setdefault(
                    (crate, version),
                    {
                        "crate": crate,
                        "version": version,
                        "source_root": full_path,
                    },
                )

    ordered = sorted(crates.values(), key=lambda item: (item["crate"], item["version"]))
    return {
        "dbgsym_deb": str(deb_path.resolve()),
        "debug_files": [debug_file.name for debug_file in debug_files],
        "crate_count": len(ordered),
        "rustc_versions": rustc_versions,
        "crates": ordered,
    }


def cargo_metadata(manifest_path: Path) -> dict:
    result = subprocess.run(
        [
            "cargo",
            "metadata",
            "--manifest-path",
            str(manifest_path),
            "--format-version",
            "1",
            "--no-deps",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def resolve_transitive_crates(manifest_paths: list[Path], registry_dir: Path) -> dict:
    manifests = list(manifest_paths)
    if registry_dir.is_dir():
        manifests.extend(sorted(registry_dir.glob("*/Cargo.toml")))

    resolved: dict[str, dict] = {}
    for manifest_path in manifests:
        metadata = cargo_metadata(manifest_path)
        for package in metadata.get("packages", []):
            pkg_name = str(package["name"])
            for dependency in package.get("dependencies", []):
                dep_name = str(dependency["name"])
                if not is_relevant(dep_name):
                    continue
                if dependency.get("path") and not dependency.get("source"):
                    continue

                requirement = str(dependency["req"]).strip()
                version = requirement.lstrip("^=").strip()
                entry = resolved.setdefault(
                    dep_name,
                    {
                        "crate": dep_name,
                        "version": version,
                        "requirement": requirement,
                        "referenced_by": set(),
                    },
                )
                entry["referenced_by"].add(pkg_name)

    return {
        "crates": [
            {
                "crate": item["crate"],
                "version": item["version"],
                "requirement": item["requirement"],
                "referenced_by": sorted(item["referenced_by"]),
            }
            for item in sorted(resolved.values(), key=lambda item: item["crate"])
        ]
    }


def is_exact_requirement(requirement: str | None) -> bool:
    return bool(requirement and requirement.strip().startswith("="))


def parse_semver(version: str) -> tuple[int, int, int, str | None]:
    core, _, suffix = version.partition("-")
    parts = core.split(".")
    while len(parts) < 3:
        parts.append("0")
    return int(parts[0]), int(parts[1]), int(parts[2]), suffix or None


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


def is_bare_major_requirement(requirement: str) -> bool:
    base = requirement.strip().lstrip("^").strip()
    return base.isdigit()


def merge_crate_lists(dbgsym_payload: dict, manifest_payloads: list[dict]) -> dict:
    merged: dict[str, dict] = {}

    for item in dbgsym_payload.get("crates", []):
        crate = str(item["crate"])
        version = str(item["version"])
        merged[crate] = {
            "crate": crate,
            "version": version,
            "requirement": f"={version}",
            "source": "dbgsym",
            "observed_version": version,
            "source_root": item.get("source_root"),
        }

    for payload in manifest_payloads:
        for item in payload.get("crates", []):
            crate = str(item["crate"])
            version = str(item["version"])
            requirement = str(item.get("requirement", f"^{version}"))
            source = str(item.get("source", "manifest"))
            current = merged.get(crate)

            if current is None:
                merged[crate] = {
                    "crate": crate,
                    "version": version,
                    "requirement": requirement,
                    "source": source,
                }
                continue

            if is_exact_requirement(requirement):
                current["version"] = version
                current["requirement"] = requirement
                current["source"] = source
                continue

            observed_version = current.get("observed_version")
            if observed_version and is_bare_major_requirement(requirement):
                major, minor, patch, _ = parse_semver(str(observed_version))
                if minor == 0 and patch == 0:
                    current["version"] = version
                    current["requirement"] = requirement
                    current["source"] = source
                    continue

            if "observed_version" not in current:
                current["version"] = version
                current["requirement"] = requirement
                current["source"] = source

    return {"crates": [merged[name] for name in sorted(merged)]}


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
    return parse_packages_index(gzip.decompress(compressed).decode("utf-8"))


def load_candidate_packages_from_downloads(downloads_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    candidates: dict[tuple[str, str], dict[str, str]] = {}
    for deb_path in sorted(downloads_dir.glob("*.deb")):
        name, sep, remainder = deb_path.name.partition("_")
        if not sep or not remainder.endswith(".deb"):
            continue
        version = remainder[:-4].rsplit("_", 1)[0]
        if not name.startswith(("rust-", "librust-")) or not version:
            continue
        candidates[(name, version)] = {
            "package": name,
            "source": "",
            "version": version,
            "filename": deb_path.name,
            "url": deb_path.as_uri(),
        }
    return candidates


def load_candidate_packages(indexes: list[str], downloads_dir: Path) -> dict[tuple[str, str], dict[str, str]]:
    candidates: dict[tuple[str, str], dict[str, str]] = {}
    candidates.update(load_candidate_packages_from_downloads(downloads_dir))
    for index_url in indexes:
        base_url = index_url.rsplit("/", 1)[0] + "/"
        try:
            packages = fetch_packages(index_url)
        except URLError as exc:
            print(f"warning: unable to fetch {index_url}: {exc}", flush=True)
            continue
        for pkg in packages:
            package_name = pkg.get("Package", "")
            filename = pkg.get("Filename", "")
            version = pkg.get("Version", "")
            if not package_name.startswith(("rust-", "librust-")) or not filename.endswith(".deb") or not version:
                continue
            candidates[(package_name, version)] = {
                "package": package_name,
                "source": pkg.get("Source", ""),
                "version": version,
                "filename": filename,
                "url": base_url + filename.rsplit("/", 1)[-1],
            }
    return candidates


def deb_base_version(version: str) -> str:
    version = version.split(":", 1)[-1]
    return version.rsplit("-", 1)[0]


def same_minor_series(left: str, right: str) -> bool:
    left_major, left_minor, _, _ = parse_semver(left)
    right_major, right_minor, _, _ = parse_semver(right)
    return (left_major, left_minor) == (right_major, right_minor)


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


def crate_to_package_names(crate_name: str) -> list[str]:
    deb_crate = crate_name.replace("_", "-")
    return [f"rust-{deb_crate}", f"librust-{deb_crate}-dev"]


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


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


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


def stage_registry(
    crate_payload: dict,
    candidates: dict[tuple[str, str], dict[str, str]],
    registry_dir: Path,
    downloads_dir: Path,
    repo_root: Path,
    package_indexes: list[str],
    crate_list_path: Path,
) -> dict:
    requested = [crate for crate in crate_payload.get("crates", []) if is_relevant(str(crate.get("crate", "")))]

    registry_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

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
        deb_path = downloads_dir / deb_name
        if not deb_path.exists():
            download(package["url"], deb_path)

        resolved_version = deb_base_version(package["version"])
        run_extract_script(repo_root, deb_path, crate_name, resolved_version, registry_dir)
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

    return {
        "crate_list": str(crate_list_path.resolve()),
        "registry_dir": str(registry_dir.resolve()),
        "downloads_dir": str(downloads_dir.resolve()),
        "package_indexes": package_indexes,
        "staged_count": len(staged),
        "missing_count": len(missing),
        "staged": staged,
        "missing": missing,
        "applied_patches": apply_local_patches(repo_root, registry_dir),
    }


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    checkout = args.checkout.resolve()
    downloads_dir = args.downloads_dir.resolve()
    state_dir = args.state_dir.resolve()
    registry_dir = args.registry_dir.resolve()
    package_indexes = args.packages_indexes or DEFAULT_PACKAGE_INDEXES
    candidates = load_candidate_packages(package_indexes, args.downloads_dir.resolve())

    state_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    registry_dir.mkdir(parents=True, exist_ok=True)

    dbgsym_payload = extract_dbgsym_crates(args.dbgsym_deb.resolve())
    dbgsym_path = state_dir / "dbgsym-crates.json"
    write_json(dbgsym_path, dbgsym_payload)

    direct_stage = stage_registry(
        dbgsym_payload,
        candidates,
        registry_dir,
        downloads_dir,
        repo_root,
        package_indexes,
        dbgsym_path,
    )
    direct_stage_path = state_dir / "staged-direct.json"
    write_json(direct_stage_path, direct_stage)

    target_manifest_paths = [checkout / relative for relative in TARGET_MANIFESTS]
    transitive_payload = resolve_transitive_crates(target_manifest_paths, registry_dir)
    transitive_path = state_dir / "target-transitive-crates.json"
    write_json(transitive_path, transitive_payload)

    final_payload = merge_crate_lists(dbgsym_payload, [transitive_payload])
    final_path = state_dir / "target-final-crates.json"
    write_json(final_path, final_payload)

    final_stage = stage_registry(
        final_payload,
        candidates,
        registry_dir,
        downloads_dir,
        repo_root,
        package_indexes,
        final_path,
    )
    final_stage_path = state_dir / "staged.json"
    write_json(final_stage_path, final_stage)

    summary = {
        "dbgsym_deb": str(args.dbgsym_deb.resolve()),
        "checkout": str(checkout),
        "downloads_dir": str(downloads_dir),
        "registry_dir": str(registry_dir),
        "state_dir": str(state_dir),
        "package_indexes": package_indexes,
        "artifacts": {
            "dbgsym_crates": str(dbgsym_path),
            "staged_direct": str(direct_stage_path),
            "target_transitive_crates": str(transitive_path),
            "target_final_crates": str(final_path),
            "staged_final": str(final_stage_path),
        },
    }
    write_json(state_dir / "summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
