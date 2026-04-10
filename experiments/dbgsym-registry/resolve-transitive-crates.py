#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


RELEVANT_PREFIXES = ("proxmox-", "pbs-")
SPECIAL_CRATES = {"pxar", "pathpatterns"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve Proxmox-family transitive crates from local Cargo manifests."
    )
    parser.add_argument(
        "--manifest-path",
        action="append",
        dest="manifest_paths",
        default=[],
        type=Path,
        help="Manifest path to inspect with cargo metadata --no-deps. May be repeated.",
    )
    parser.add_argument(
        "--registry-dir",
        type=Path,
        help="Optional directory of extracted crates. Each <crate-version>/Cargo.toml will be scanned.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def is_relevant(crate_name: str) -> bool:
    return crate_name == "pxar" or crate_name in SPECIAL_CRATES or crate_name.startswith(RELEVANT_PREFIXES)


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


def main() -> int:
    args = parse_args()
    manifest_paths = [path.resolve() for path in args.manifest_paths]
    if args.registry_dir:
        for cargo_toml in sorted(args.registry_dir.resolve().glob("*/Cargo.toml")):
            manifest_paths.append(cargo_toml)

    resolved: dict[str, dict] = {}
    for manifest_path in manifest_paths:
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

    payload = {
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
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
