#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


PROXMOX_PREFIXES = ("proxmox-", "pbs-")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve direct Proxmox crate dependencies from an upstream checkout."
    )
    parser.add_argument("source_tree", type=Path, help="Path to the upstream proxmox-backup checkout")
    parser.add_argument(
        "--include-dev-dependencies",
        action="store_true",
        help="Include dev-dependencies in the resolved crate set",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON output to this path instead of stdout",
    )
    return parser.parse_args()


def is_relevant_crate(crate_name: str) -> bool:
    return crate_name == "pxar" or crate_name.startswith(PROXMOX_PREFIXES)


def load_metadata(source_tree: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "cargo",
            "metadata",
            "--manifest-path",
            str(source_tree / "Cargo.toml"),
            "--format-version",
            "1",
            "--no-deps",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def manifest_path_to_member_name(packages: list[dict[str, Any]]) -> dict[str, str]:
    return {str(package["manifest_path"]): str(package["name"]) for package in packages}


def main() -> int:
    args = parse_args()
    source_tree = args.source_tree.resolve()
    metadata = load_metadata(source_tree)
    packages = metadata.get("packages", [])
    if not isinstance(packages, list):
        raise SystemExit("cargo metadata did not return a package list")

    local_packages = {str(package["name"]) for package in packages}
    manifest_names = manifest_path_to_member_name(packages)
    workspace_root = str(metadata["workspace_root"])

    resolved: dict[str, dict[str, Any]] = {}

    for package in packages:
        package_name = str(package["name"])
        for dependency in package.get("dependencies", []):
            crate_name = str(dependency.get("rename") or dependency["name"])
            actual_package_name = str(dependency["name"])
            kind = dependency.get("kind") or "normal"

            if kind == "dev" and not args.include_dev_dependencies:
                continue
            if actual_package_name in local_packages:
                continue
            if dependency.get("path") and not dependency.get("source"):
                continue
            if not is_relevant_crate(actual_package_name):
                continue

            raw_requirement = str(dependency["req"]).strip()
            version_req = raw_requirement.lstrip("^=").strip()
            if not version_req:
                raise SystemExit(f"dependency {actual_package_name!r} in {package_name!r} has no usable version requirement")

            target = dependency.get("target")
            section = f"target.{target}.{kind}" if target else kind
            entry = resolved.setdefault(
                actual_package_name,
                {
                    "crate": actual_package_name,
                    "requirement": raw_requirement,
                    "version": version_req,
                    "features": set(),
                    "kind": set(),
                    "referenced_by": set(),
                    "sections": set(),
                    "renamed_as": set(),
                },
            )
            if entry["requirement"] != raw_requirement:
                raise SystemExit(
                    f"conflicting requirements for crate {actual_package_name}: {entry['requirement']} vs {raw_requirement}"
                )
            if entry["version"] != version_req:
                raise SystemExit(
                    f"conflicting versions for crate {actual_package_name}: {entry['version']} vs {version_req}"
                )
            entry["kind"].add(kind)
            entry["referenced_by"].add(package_name)
            entry["sections"].add(section)
            if crate_name != actual_package_name:
                entry["renamed_as"].add(crate_name)
            for feature in dependency.get("features", []):
                entry["features"].add(str(feature))

    workspace_members = sorted(
        {
            manifest_names[manifest_path]
            for manifest_path in manifest_names
            if manifest_path.startswith(workspace_root)
        }
    )
    output = {
        "source_tree": str(source_tree),
        "workspace_members": workspace_members,
        "crates": [
            {
                **{
                    key: value
                    for key, value in crate.items()
                    if key not in {"features", "kind", "referenced_by", "renamed_as", "sections"}
                },
                "features": sorted(crate["features"]),
                "kind": sorted(crate["kind"]),
                "referenced_by": sorted(crate["referenced_by"]),
                "renamed_as": sorted(crate["renamed_as"]),
                "sections": sorted(crate["sections"]),
            }
            for crate in sorted(resolved.values(), key=lambda item: item["crate"])
        ],
    }

    serialized = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    else:
        sys.stdout.write(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
