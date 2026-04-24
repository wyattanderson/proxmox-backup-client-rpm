#!/usr/bin/env bash

set -euo pipefail

repo_root=$(realpath "$(dirname "$0")/..")
topdir="${1:-${repo_root}/dist/rpmbuild}"
sources_dir="${topdir}/SOURCES"
specs_dir="${topdir}/SPECS"
srpms_dir="${topdir}/SRPMS"
work_dir="${repo_root}/dist/srpm-work"
downloads_dir="${repo_root}/downloads"
checkout="${repo_root}/state/dbgsym-registry/proxmox-backup-src"

mkdir -p "${sources_dir}" "${specs_dir}" "${srpms_dir}" "${work_dir}" "${downloads_dir}" "$(dirname "${checkout}")"

dbgsym_deb="${downloads_dir}/proxmox-backup-client-dbgsym_4.1.8-1_amd64.deb"

if [[ ! -f "${dbgsym_deb}" ]]; then
    curl -LfsS -o "${dbgsym_deb}" \
      http://download.proxmox.com/debian/pbs/dists/trixie/pbs-no-subscription/binary-amd64/proxmox-backup-client-dbgsym_4.1.8-1_amd64.deb
fi

if [[ ! -d "${checkout}" ]]; then
    git clone --depth 1 --branch v4.1.8 \
      git://git.proxmox.com/git/proxmox-backup.git \
      "${checkout}"
fi

python3 "${repo_root}/scripts/prepare-srpm-sources.py" \
  --dbgsym-deb "${dbgsym_deb}" \
  --checkout "${checkout}" \
  --downloads-dir "${downloads_dir}" \
  --work-dir "${work_dir}" \
  --output-dir "${sources_dir}" \
  --repo-root "${repo_root}"

cp "${repo_root}/proxmox-backup-client.spec" "${specs_dir}/"

rpmbuild -bs \
  --define "_topdir ${topdir}" \
  --undefine dist \
  "${specs_dir}/proxmox-backup-client.spec"
