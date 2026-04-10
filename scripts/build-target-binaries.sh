#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <proxmox-backup-checkout> <staged-registry-dir> [cargo args...]" >&2
    exit 2
fi

checkout=$(realpath "$1")
registry_dir=$(realpath "$2")
shift 2

cargo_home="${checkout}/.cargo-rhel9-build"
config_path="${cargo_home}/config.toml"

mkdir -p "$cargo_home"

python3 - "$registry_dir" "$config_path" <<'PY'
import sys
from pathlib import Path

registry_dir = Path(sys.argv[1])
config_path = Path(sys.argv[2])
selected = {}

for crate_dir in sorted(registry_dir.iterdir()):
    if not crate_dir.is_dir():
        continue
    name, _, version = crate_dir.name.rpartition("-")
    if not name or not version:
        continue
    selected[name] = crate_dir

patches = [f'"{name}" = {{ path = "{selected[name]}" }}' for name in sorted(selected)]
content = "[patch.crates-io]\n" + "\n".join(patches) + "\n"
config_path.write_text(content, encoding="utf-8")
PY

export CARGO_HOME="$cargo_home"

if [[ $# -eq 0 ]]; then
    cargo build --manifest-path "${checkout}/Cargo.toml" --release \
        --package proxmox-backup-client --bin proxmox-backup-client \
        --package pxar-bin --bin pxar
else
    cargo build --manifest-path "${checkout}/Cargo.toml" --release "$@"
fi
