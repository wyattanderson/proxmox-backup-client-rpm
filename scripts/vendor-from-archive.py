#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PACKAGE_INDEXES = [
    "http://download.proxmox.com/debian/devel/dists/bullseye/main/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/devel/dists/bookworm/main/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/devel/dists/trixie/main/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/pbs/dists/bullseye/pbs-no-subscription/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/pbs/dists/bullseye/pbstest/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/pbs/dists/bookworm/pbs-no-subscription/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/pbs/dists/bookworm/pbstest/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/pbs/dists/trixie/pbs-no-subscription/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/pbs/dists/trixie/pbstest/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/dists/bullseye/pve-no-subscription/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/dists/bullseye/pvetest/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/dists/bookworm/pve-no-subscription/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/dists/bookworm/pvetest/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/dists/trixie/pve-no-subscription/binary-amd64/Packages.gz",
    "http://download.proxmox.com/debian/dists/trixie/pvetest/binary-amd64/Packages.gz",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Vendor Proxmox Rust crates from published Debian package indexes."
    )
    parser.add_argument("--crate-manifest", required=True, type=Path)
    parser.add_argument("--vendor-dir", required=True, type=Path)
    parser.add_argument("--downloads-dir", required=True, type=Path)
    parser.add_argument("--output-manifest", required=True, type=Path)
    parser.add_argument("--upstream-tag", required=True)
    parser.add_argument("--upstream-commit", required=True)
    parser.add_argument("--upstream-url", required=True)
    parser.add_argument(
        "--packages-index",
        dest="packages_indexes",
        action="append",
        default=[],
        help="Append a Packages.gz URL to search. If omitted, built-in Proxmox defaults are used.",
    )
    parser.add_argument(
        "--extract-script",
        type=Path,
        default=Path(__file__).with_name("extract-deb-crate.sh"),
    )
    return parser.parse_args()


def debian_version_key(version: str) -> tuple[str, str, str]:
    epoch = "0"
    upstream_revision = version
    if ":" in upstream_revision:
        epoch, upstream_revision = upstream_revision.split(":", 1)
    if "-" in upstream_revision:
        upstream, revision = upstream_revision.rsplit("-", 1)
    else:
        upstream, revision = upstream_revision, ""
    return epoch, upstream, revision


def order_char(char: str) -> int:
    if char == "~":
        return -1
    if char.isalnum():
        return ord(char)
    return ord(char) + 256


def compare_non_digit(left: str, right: str) -> int:
    left_index = 0
    right_index = 0
    while left_index < len(left) or right_index < len(right):
        left_char = left[left_index] if left_index < len(left) else ""
        right_char = right[right_index] if right_index < len(right) else ""
        left_order = order_char(left_char) if left_char else 0
        right_order = order_char(right_char) if right_char else 0
        if left_order != right_order:
            return -1 if left_order < right_order else 1
        left_index += 1 if left_char else 0
        right_index += 1 if right_char else 0
    return 0


def compare_digit(left: str, right: str) -> int:
    left = left.lstrip("0") or "0"
    right = right.lstrip("0") or "0"
    if len(left) != len(right):
        return -1 if len(left) < len(right) else 1
    if left != right:
        return -1 if left < right else 1
    return 0


def split_version_part(part: str) -> Iterable[tuple[str, str]]:
    index = 0
    while index < len(part):
        start = index
        while index < len(part) and not part[index].isdigit():
            index += 1
        if start != index:
            yield ("non-digit", part[start:index])
        start = index
        while index < len(part) and part[index].isdigit():
            index += 1
        if start != index:
            yield ("digit", part[start:index])


def compare_version_part(left: str, right: str) -> int:
    left_parts = list(split_version_part(left))
    right_parts = list(split_version_part(right))
    max_len = max(len(left_parts), len(right_parts))
    for index in range(max_len):
        left_kind, left_value = left_parts[index] if index < len(left_parts) else ("non-digit", "")
        right_kind, right_value = right_parts[index] if index < len(right_parts) else ("non-digit", "")
        if left_kind == "digit" and right_kind == "digit":
            comparison = compare_digit(left_value, right_value)
        else:
            comparison = compare_non_digit(left_value, right_value)
        if comparison:
            return comparison
    return 0


def compare_debian_versions(left: str, right: str) -> int:
    left_epoch, left_upstream, left_revision = debian_version_key(left)
    right_epoch, right_upstream, right_revision = debian_version_key(right)

    epoch_cmp = compare_digit(left_epoch, right_epoch)
    if epoch_cmp:
        return epoch_cmp

    upstream_cmp = compare_version_part(left_upstream, right_upstream)
    if upstream_cmp:
        return upstream_cmp

    return compare_version_part(left_revision, right_revision)


def parse_upstream_version(package_version: str) -> str:
    _, upstream, _ = debian_version_key(package_version)
    return upstream


def parse_semver(version: str) -> tuple[int, int, int, str | None]:
    core, sep, suffix = version.partition("-")
    parts = core.split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        major, minor, patch = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise ValueError(f"unsupported semver value: {version}") from exc
    return major, minor, patch, suffix or None


def requirement_base(requirement: str) -> str:
    requirement = requirement.strip()
    for prefix in ("^", "="):
        if requirement.startswith(prefix):
            return requirement[len(prefix) :].strip()
    return requirement


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


def parse_deb822_paragraphs(text: str) -> list[dict[str, str]]:
    paragraphs: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_key: str | None = None

    for raw_line in text.splitlines():
        if not raw_line:
            if current:
                paragraphs.append(current)
                current = {}
                current_key = None
            continue
        if raw_line.startswith((" ", "\t")):
            if current_key is None:
                raise ValueError(f"continuation line without a key: {raw_line!r}")
            current[current_key] += "\n" + raw_line[1:]
            continue
        key, value = raw_line.split(":", 1)
        current_key = key
        current[key] = value.strip()

    if current:
        paragraphs.append(current)
    return paragraphs


def parse_provides(value: str) -> set[str]:
    provides: set[str] = set()
    for item in value.split(","):
        token = item.strip()
        if not token:
            continue
        token = token.split("(", 1)[0].strip()
        if token:
            provides.add(token)
    return provides


def virtual_provide_names(crate_name: str, requirement: str) -> set[str]:
    base = requirement_base(requirement)
    major, minor, patch, _ = parse_semver(base)
    names = {
        f"librust-{crate_name}-dev",
        f"librust-{crate_name}-{major}+default-dev",
        f"librust-{crate_name}-{major}-dev",
    }
    if minor or patch:
        names.add(f"librust-{crate_name}-{major}.{minor}+default-dev")
        names.add(f"librust-{crate_name}-{major}.{minor}-dev")
    if patch:
        names.add(f"librust-{crate_name}-{major}.{minor}.{patch}+default-dev")
        names.add(f"librust-{crate_name}-{major}.{minor}.{patch}-dev")
    return names


def load_package_index(url: str) -> list[dict[str, str]]:
    with urllib.request.urlopen(url) as response:
        payload = response.read()
    if url.endswith(".gz"):
        payload = gzip.decompress(payload)
    return parse_deb822_paragraphs(payload.decode("utf-8"))


def repo_root_from_index(url: str) -> str:
    marker = "/dists/"
    if marker not in url:
        raise ValueError(f"cannot derive repository root from {url}")
    return url.split(marker, 1)[0]


def download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def select_candidate(
    crate_name: str,
    crate_requirement: str,
    indexes: list[tuple[str, list[dict[str, str]]]],
) -> tuple[str, dict[str, str], str]:
    package_name = f"librust-{crate_name}-dev"
    matches: list[tuple[str, dict[str, str]]] = []

    for index_url, entries in indexes:
        for entry in entries:
            if entry.get("Package") != package_name:
                continue
            version = entry.get("Version")
            filename = entry.get("Filename")
            if not version or not filename:
                continue
            upstream_version = parse_upstream_version(version)
            provides = parse_provides(entry.get("Provides", ""))
            if not (virtual_provide_names(crate_name, crate_requirement) & provides):
                continue
            if not semver_satisfies(crate_requirement, upstream_version):
                continue
            matches.append((index_url, entry))

    if not matches:
        raise RuntimeError(f"missing archive package for crate {crate_name} requirement {crate_requirement}")

    best_index_url, best_entry = matches[0]
    for candidate_index_url, candidate_entry in matches[1:]:
        if compare_debian_versions(candidate_entry["Version"], best_entry["Version"]) > 0:
            best_index_url, best_entry = candidate_index_url, candidate_entry
    return best_index_url, best_entry, parse_upstream_version(best_entry["Version"])


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_crate_requests(path: Path) -> list[dict[str, Any]]:
    payload = read_json(path)
    crates = payload.get("crates")
    if not isinstance(crates, list):
        raise ValueError(f"crate manifest at {path} has no 'crates' array")
    return crates


def extract_crate(extract_script: Path, deb_path: Path, crate_name: str, crate_version: str, vendor_dir: Path) -> None:
    subprocess.run(
        [str(extract_script), str(deb_path), crate_name, crate_version, str(vendor_dir)],
        check=True,
    )


def load_vcs_info(crate_dir: Path) -> tuple[str | None, str | None]:
    vcs_info_path = crate_dir / ".cargo_vcs_info.json"
    if not vcs_info_path.exists():
        return None, None
    payload = read_json(vcs_info_path)
    git_info = payload.get("git", {})
    commit = git_info.get("sha1")
    path_in_vcs = payload.get("path_in_vcs")
    return commit, path_in_vcs


def main() -> int:
    args = parse_args()
    packages_indexes = args.packages_indexes or DEFAULT_PACKAGE_INDEXES
    crate_requests = load_crate_requests(args.crate_manifest)

    args.vendor_dir.mkdir(parents=True, exist_ok=True)
    args.downloads_dir.mkdir(parents=True, exist_ok=True)
    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)

    loaded_indexes: list[tuple[str, list[dict[str, str]]]] = []
    for index_url in packages_indexes:
        try:
            loaded_indexes.append((index_url, load_package_index(index_url)))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise

    if not loaded_indexes:
        raise SystemExit("no package indexes could be loaded")

    vendor_entries: list[dict[str, Any]] = []
    for crate_request in crate_requests:
        crate_name = str(crate_request["crate"])
        crate_requirement = str(crate_request.get("requirement", crate_request["version"]))
        index_url, package_entry, crate_version = select_candidate(crate_name, crate_requirement, loaded_indexes)
        repo_root = repo_root_from_index(index_url)
        filename = package_entry["Filename"]
        archive_url = f"{repo_root}/{filename}"
        deb_path = args.downloads_dir / Path(filename).name

        if not deb_path.exists():
            download_file(archive_url, deb_path)

        extract_crate(args.extract_script.resolve(), deb_path, crate_name, crate_version, args.vendor_dir)
        crate_dir = args.vendor_dir / f"{crate_name}-{crate_version}"
        vcs_commit, path_in_vcs = load_vcs_info(crate_dir)

        vendor_entries.append(
            {
                "archive_url": archive_url,
                "crate": crate_name,
                "crate_dir": str(crate_dir),
                "deb_filename": Path(filename).name,
                "features": crate_request.get("features", []),
                "kind": crate_request.get("kind", []),
                "package": package_entry["Package"],
                "package_version": package_entry["Version"],
                "path_in_vcs": path_in_vcs,
                "referenced_by": crate_request.get("referenced_by", []),
                "requirement": crate_requirement,
                "sections": crate_request.get("sections", []),
                "source_index": index_url,
                "vcs_commit": vcs_commit,
                "version": crate_version,
            }
        )

    manifest = {
        "archive_indexes": packages_indexes,
        "bootstrap_timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "resolved_crates": crate_requests,
        "tag": args.upstream_tag,
        "upstream_commit": args.upstream_commit,
        "upstream_url": args.upstream_url,
        "vendor_entries": sorted(vendor_entries, key=lambda item: item["crate"]),
    }
    args.output_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
