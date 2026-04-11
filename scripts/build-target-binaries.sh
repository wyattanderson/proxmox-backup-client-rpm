#!/usr/bin/env bash

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <proxmox-backup-checkout> <staged-registry-dir> [vendored-crates-dir] [cargo args...]" >&2
    exit 2
fi

checkout=$(realpath "$1")
registry_dir=$(realpath "$2")
shift 2

vendor_dir=""
if [[ $# -gt 0 && -d "$1" ]]; then
    vendor_dir=$(realpath "$1")
    shift
fi

cargo_home="${checkout}/.cargo-rhel9-build"
config_path="${cargo_home}/config.toml"
project_cargo_dir="${checkout}/.cargo"
project_config="${project_cargo_dir}/config.toml"
project_config_backup="${project_cargo_dir}/config.toml.rpm-backup"

mkdir -p "$cargo_home"

restore_project_config() {
    if [[ -f "${project_config_backup}" ]]; then
        mv -f "${project_config_backup}" "${project_config}"
    fi
}
trap restore_project_config EXIT

if [[ -f "${project_config}" ]]; then
    mv -f "${project_config}" "${project_config_backup}"
fi

python3 - "$registry_dir" "$config_path" "$vendor_dir" <<'PY'
import sys
from pathlib import Path

registry_dir = Path(sys.argv[1])
config_path = Path(sys.argv[2])
vendor_dir = Path(sys.argv[3]) if sys.argv[3] else None
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
if vendor_dir is not None:
    content += (
        "\n[source.crates-io]\n"
        'replace-with = "vendored-sources"\n'
        "\n[source.vendored-sources]\n"
        f'directory = "{vendor_dir}"\n'
    )
config_path.write_text(content, encoding="utf-8")
PY

export CARGO_HOME="$cargo_home"

if [[ $# -eq 0 ]]; then
    cargo build --manifest-path "${checkout}/Cargo.toml" --release ${vendor_dir:+--locked} \
        --package proxmox-backup-client --bin proxmox-backup-client \
        --package pxar-bin --bin pxar
else
    cargo build --manifest-path "${checkout}/Cargo.toml" --release ${vendor_dir:+--locked} "$@"
fi
