#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path


CRATE_PATH_RE = re.compile(r"(/usr/share/cargo/registry/([^/ ]+)-([0-9][^/ ]*))")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract crate/version roots from a proxmox-backup-client dbgsym package."
    )
    parser.add_argument("--deb", required=True, type=Path, help="Path to the dbgsym .deb file")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path. Defaults to stdout.",
    )
    return parser.parse_args()


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
        subprocess.run(
            ["ar", "p", str(deb_path), data_member],
            check=True,
            stdout=archive_file,
        )

    subprocess.run(
        ["tar", "-xaf", str(archive_path), "-C", str(extract_root)],
        check=True,
    )

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


def collect_crates(debug_files: list[Path]) -> tuple[list[dict[str, str]], list[str]]:
    crates: dict[tuple[str, str], dict[str, str]] = {}
    rustc_versions: list[str] = []

    for debug_file in debug_files:
        comment = rustc_comment(debug_file)
        if comment and comment not in rustc_versions:
            rustc_versions.append(comment)

        for match in CRATE_PATH_RE.finditer(readelf_decodedline(debug_file)):
            full_path, crate, version = match.groups()
            key = (crate, version)
            crates.setdefault(
                key,
                {
                    "crate": crate,
                    "version": version,
                    "source_root": full_path,
                },
            )

    ordered = sorted(crates.values(), key=lambda item: (item["crate"], item["version"]))
    return ordered, rustc_versions


def main() -> int:
    args = parse_args()
    deb_path = args.deb.resolve()

    with tempfile.TemporaryDirectory(prefix="dbgsym-registry-") as temp_dir:
        debug_files = extract_debug_files(deb_path, Path(temp_dir))
        crates, rustc_versions = collect_crates(debug_files)

    payload = {
        "dbgsym_deb": str(deb_path),
        "debug_files": [debug_file.name for debug_file in debug_files],
        "crate_count": len(crates),
        "rustc_versions": rustc_versions,
        "crates": crates,
    }
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
