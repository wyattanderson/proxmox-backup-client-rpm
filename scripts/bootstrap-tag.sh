#!/usr/bin/env bash

set -euo pipefail

usage() {
    cat <<'EOF'
usage: scripts/bootstrap-tag.sh <tag> [options]

Options:
  --archive-index URL    Add a Packages.gz URL to search for crate packages.
  --output-dir DIR       Write generated state under DIR instead of state/<tag>.
  --upstream-url URL     Override the proxmox-backup git remote.
  --keep-checkout        Reuse an existing checkout directory if present.
EOF
}

if [[ $# -lt 1 ]]; then
    usage >&2
    exit 2
fi

tag=""
output_dir=""
upstream_url="git://git.proxmox.com/git/proxmox-backup.git"
keep_checkout=0
declare -a archive_indexes=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --archive-index)
            archive_indexes+=("$2")
            shift 2
            ;;
        --output-dir)
            output_dir=$2
            shift 2
            ;;
        --upstream-url)
            upstream_url=$2
            shift 2
            ;;
        --keep-checkout)
            keep_checkout=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "unknown option: $1" >&2
            exit 2
            ;;
        *)
            if [[ -n "$tag" ]]; then
                echo "tag already set to $tag, unexpected argument: $1" >&2
                exit 2
            fi
            tag=$1
            shift
            ;;
    esac
done

if [[ -z "$tag" ]]; then
    echo "missing required tag argument" >&2
    exit 2
fi

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
output_dir=${output_dir:-"${repo_root}/state/${tag}"}
checkout_dir="${output_dir}/upstream"
downloads_dir="${output_dir}/downloads"
vendor_dir="${output_dir}/vendor"
manifest_dir="${output_dir}/manifest"
crate_manifest="${manifest_dir}/resolved-crates.json"
vendor_manifest="${manifest_dir}/sources.json"

mkdir -p "$manifest_dir"

if [[ -d "$checkout_dir/.git" && "$keep_checkout" -eq 1 ]]; then
    git -C "$checkout_dir" fetch --tags --force "$upstream_url"
    git -C "$checkout_dir" checkout --force "$tag"
else
    rm -rf "$checkout_dir"
    git clone --branch "$tag" --depth 1 "$upstream_url" "$checkout_dir"
fi

upstream_commit=$(git -C "$checkout_dir" rev-parse HEAD)

python3 "${repo_root}/scripts/resolve-crates.py" \
    --output "$crate_manifest" \
    "$checkout_dir"

vendor_args=(
    python3 "${repo_root}/scripts/vendor-from-archive.py"
    --crate-manifest "$crate_manifest"
    --vendor-dir "$vendor_dir"
    --downloads-dir "$downloads_dir"
    --output-manifest "$vendor_manifest"
    --upstream-tag "$tag"
    --upstream-commit "$upstream_commit"
    --upstream-url "$upstream_url"
)

if [[ ${#archive_indexes[@]} -gt 0 ]]; then
    for archive_index in "${archive_indexes[@]}"; do
        vendor_args+=(--packages-index "$archive_index")
    done
fi

"${vendor_args[@]}"

printf 'bootstrap complete\n  tag: %s\n  commit: %s\n  crate manifest: %s\n  sources manifest: %s\n' \
    "$tag" \
    "$upstream_commit" \
    "$crate_manifest" \
    "$vendor_manifest"
