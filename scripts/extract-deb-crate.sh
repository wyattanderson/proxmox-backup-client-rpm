#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 4 ]]; then
    echo "usage: $0 <deb-path> <crate-name> <crate-version> <output-dir>" >&2
    exit 2
fi

deb_path=$1
crate_name=$2
crate_version=$3
output_dir=$4
crate_dir_name="${crate_name}-${crate_version}"

tmpdir=$(mktemp -d)
cleanup() {
    rm -rf "$tmpdir"
}
trap cleanup EXIT

data_member=$(ar t "$deb_path" | awk '/^data\.tar\./ { print; exit }')
if [[ -z "${data_member}" ]]; then
    echo "no data.tar.* member found in ${deb_path}" >&2
    exit 1
fi

archive_path="${tmpdir}/${data_member}"
extract_root="${tmpdir}/root"

mkdir -p "$extract_root" "$output_dir"
ar p "$deb_path" "$data_member" > "$archive_path"
tar -xaf "$archive_path" -C "$extract_root"

source_dir="${extract_root}/usr/share/cargo/registry/${crate_dir_name}"
if [[ ! -d "$source_dir" ]]; then
    echo "crate directory ${crate_dir_name} not found in ${deb_path}" >&2
    exit 1
fi

rm -rf "${output_dir:?}/${crate_dir_name}"
cp -a "$source_dir" "$output_dir/"
