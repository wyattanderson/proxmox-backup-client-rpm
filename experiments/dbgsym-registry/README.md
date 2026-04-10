# Dbgsym Registry Experiment

This experiment explores whether the published `proxmox-backup-client-dbgsym`
package contains enough Rust debug information to reconstruct a close-enough
crate source layout for building `proxmox-backup-client` on RHEL 9.

The approach is:

1. Extract `crate-version` source roots from the `.debug` ELF DWARF line tables.
2. Stage Proxmox-published crates from Debian `.deb` packages into a local cargo
   registry-like tree.
3. Apply minimal local patches to staged Proxmox crates.
4. Point a local checkout of `proxmox-backup` at that staged tree and let Cargo
   resolve the remaining crates from crates.io.

## Files

- `extract-crate-versions.py`
  Reads a dbgsym `.deb` and emits the unique crate/version list found in DWARF.
- `stage-proxmox-registry.py`
  Downloads Proxmox crate packages from package indexes, extracts matching
  crates into a local registry tree, and records what was staged.
- `attempt-build.sh`
  Creates a cargo config pointing selected crates at the staged registry and
  runs `cargo build` from an upstream checkout.

## Typical flow

```bash

curl -LO --output-dir state/dbgsym-registry \
  http://download.proxmox.com/debian/pbs-client/dists/trixie/main/binary-amd64/proxmox-backup-client-dbgsym_4.1.5-1_amd64.deb

git clone git://git.proxmox.com/git/proxmox-backup.git \
  --depth 1 \
  --branch v4.1.5 \
  state/dbgsym-registry/proxmox-backup-src

python3 experiments/dbgsym-registry/extract-crate-versions.py \
  --deb state/dbgsym-registry/proxmox-backup-client-dbgsym_4.1.5-1_amd64.deb \
  --output state/dbgsym-registry/crates.json

python3 experiments/dbgsym-registry/resolve-transitive-crates.py \
  --manifest-path state/dbgsym-registry/proxmox-backup-src/proxmox-backup-client/Cargo.toml \
  --manifest-path state/dbgsym-registry/proxmox-backup-src/pxar-bin/Cargo.toml \
  --registry-dir state/dbgsym-registry/registry \
  --output state/dbgsym-registry/target-transitive-crates.json

python3 experiments/dbgsym-registry/merge-crate-lists.py \
  --dbgsym state/dbgsym-registry/crates.json \
  --manifest-list state/dbgsym-registry/target-transitive-crates.json \
  --output state/dbgsym-registry/target-final-crates.json

python3 experiments/dbgsym-registry/stage-proxmox-registry.py \
  --crate-list state/dbgsym-registry/target-final-crates.json \
  --registry-dir state/dbgsym-registry/registry \
  --downloads-dir state/dbgsym-registry/downloads \
  --manifest-out state/dbgsym-registry/staged.json

experiments/dbgsym-registry/attempt-build.sh \
  state/dbgsym-registry/proxmox-backup-src \
  state/dbgsym-registry/registry
```

## Notes

- This is intentionally separate from the existing offline vendor workflow.
- The staged tree is cargo-compatible only for patched path overrides. It does
  not try to emulate the crates.io index.
- The debug-derived crate list is useful because Debian preserved source roots
  like `/usr/share/cargo/registry/<crate>-<version>/...` in DWARF.
