# proxmox-backup-client-rpm

This repository packages `proxmox-backup-client` and `pxar` for RHEL 9.

The supported build path is driven by `all.sh`. It reconstructs a close-enough
Proxmox Rust crate registry from:

- the published `proxmox-backup-client-dbgsym` package, which preserves crate
  source roots in DWARF
- Proxmox `librust-*` crate packages from the Debian archive
- a checkout of the upstream `proxmox-backup` repository at `v4.1.5`

The result is a local registry tree that can be patched and used to build only
the two binaries we care about on RHEL 9:

- `proxmox-backup-client`
- `pxar`

## Prerequisites

Install the usual build tools first. At minimum the workflow expects:

- `bash`
- `python3`
- `cargo`
- `git`
- `curl`
- `ar`
- `tar`
- `patch`
- `readelf`

Network access is required the first time so the script can download:

- `proxmox-backup-client-dbgsym_4.1.5-1_amd64.deb`
- the upstream `proxmox-backup` checkout
- Proxmox `librust-*` crate packages needed for the staged registry
- crates.io dependencies that are not provided by Proxmox

## Build Flow

Run the whole process from a clean checkout:

```bash
./all.sh
```

`all.sh` performs these steps:

1. Download the `proxmox-backup-client` dbgsym package into `downloads/`.
2. Clone `git://git.proxmox.com/git/proxmox-backup.git` at tag `v4.1.5` into
   `state/dbgsym-registry/proxmox-backup-src/`.
3. Run `scripts/prepare-dbgsym-registry.py` to generate a local crate registry
   in `state/dbgsym-registry/registry/`.
4. Run `scripts/build-target-binaries.sh` to build `proxmox-backup-client` and
   `pxar` from the upstream checkout, with staged Proxmox crates patched into
   Cargo via `[patch.crates-io]`.

Built binaries end up under:

- `state/dbgsym-registry/proxmox-backup-src/target/release/proxmox-backup-client`
- `state/dbgsym-registry/proxmox-backup-src/target/release/pxar`

## Why The Registry Is Staged Twice

The two-stage preparation is intentional and is now codified in
`scripts/prepare-dbgsym-registry.py`.

The first pass stages the direct Proxmox-family crates observed in the dbgsym
package. That gives us enough local source to inspect the target crates'
dependency closure accurately.

The second pass resolves the transitive Proxmox-family crate set reachable from:

- `proxmox-backup-client/Cargo.toml`
- `pxar-bin/Cargo.toml`

Then it merges that closure with the dbgsym observations and restages the
registry with the final crate selection.

## Version Selection Heuristic

By default, dbgsym-observed crate versions are treated as authoritative.

There is one narrow relaxation rule: if a dependency requirement is only a bare
major such as `1` or `^1`, and the dbgsym-observed version is an `x.0.0`
baseline, the resolver may upgrade within that same major line. This exists so
`pxar` can move from observed `1.0.0` to `1.0.1` without broadly floating other
crate versions.

## Generated State

`scripts/prepare-dbgsym-registry.py` writes machine-readable state into
`state/dbgsym-registry/`, including:

- `dbgsym-crates.json`
- `staged-direct.json`
- `target-transitive-crates.json`
- `target-final-crates.json`
- `staged.json`
- `summary.json`

These files make it easier to inspect the selected crate set and debug version
resolution changes.
