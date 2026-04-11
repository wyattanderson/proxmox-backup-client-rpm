#!/bin/bash

set -xe

mkdir -p state/dbgsym-registry/ downloads/

[ ! -e downloads/proxmox-backup-client-dbgsym_4.1.5-1_amd64.deb ] && curl -LO --output-dir downloads \
  http://download.proxmox.com/debian/pbs-client/dists/trixie/main/binary-amd64/proxmox-backup-client-dbgsym_4.1.5-1_amd64.deb

[ ! -d state/dbgsym-registry/proxmox-backup-src ] && git clone git://git.proxmox.com/git/proxmox-backup.git \
  --depth 1 \
  --branch v4.1.5 \
  state/dbgsym-registry/proxmox-backup-src

python3 scripts/prepare-dbgsym-registry.py \
  --dbgsym-deb downloads/proxmox-backup-client-dbgsym_4.1.5-1_amd64.deb \
  --checkout state/dbgsym-registry/proxmox-backup-src \
  --downloads-dir downloads \
  --state-dir state/dbgsym-registry \
  --registry-dir state/dbgsym-registry/registry \
  --repo-root .

scripts/build-target-binaries.sh \
  state/dbgsym-registry/proxmox-backup-src \
  state/dbgsym-registry/registry
