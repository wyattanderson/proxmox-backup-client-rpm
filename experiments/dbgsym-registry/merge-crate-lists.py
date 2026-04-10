#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge dbgsym-observed crate versions with manifest-derived requirements."
    )
    parser.add_argument("--dbgsym", required=True, type=Path)
    parser.add_argument(
        "--manifest-list",
        action="append",
        dest="manifest_lists",
        default=[],
        type=Path,
        help="JSON file with a top-level 'crates' list.",
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def load_crates(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    crates = data.get("crates")
    if not isinstance(crates, list):
        raise SystemExit(f"{path} does not contain a top-level 'crates' list")
    return crates


def is_exact(requirement: str | None) -> bool:
    return bool(requirement and requirement.strip().startswith("="))


def parse_semver(version: str) -> tuple[int, int, int]:
    parts = version.split(".", 2)
    while len(parts) < 3:
        parts.append("0")
    return int(parts[0]), int(parts[1]), int(parts[2])


def is_bare_major_requirement(requirement: str) -> bool:
    base = requirement.strip().lstrip("^").strip()
    return base.isdigit()


def main() -> int:
    args = parse_args()

    merged: dict[str, dict] = {}

    for item in load_crates(args.dbgsym):
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

    for manifest_path in args.manifest_lists:
        for item in load_crates(manifest_path):
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

            if is_exact(requirement):
                current["version"] = version
                current["requirement"] = requirement
                current["source"] = source
                continue

            observed_version = current.get("observed_version")
            if observed_version and is_bare_major_requirement(requirement):
                major, minor, patch = parse_semver(str(observed_version))
                if minor == 0 and patch == 0:
                    # Minimal relaxation heuristic: if the dbgsym saw an x.0.0
                    # crate version but the manifest only asks for a bare major,
                    # allow a patch-level upgrade within that major line.
                    current["version"] = version
                    current["requirement"] = requirement
                    current["source"] = source
                    continue

            if "observed_version" not in current:
                current["version"] = version
                current["requirement"] = requirement
                current["source"] = source

    payload = {"crates": [merged[name] for name in sorted(merged)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
