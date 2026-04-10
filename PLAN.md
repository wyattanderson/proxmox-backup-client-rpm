# proxmox-backup-client RPM Plan

## Goal

Build `proxmox-backup-client` and `pxar` for RHEL 9 from a specific upstream `proxmox-backup` tag with as much fidelity to the Debian build as practical, while keeping the maintenance burden close to zero for new upstream tags.

The intended steady-state workflow is:

1. Proxmox publishes a new `proxmox-backup` tag.
2. This repository is pointed at that tag, or CI discovers it automatically.
3. A bootstrap step reconstructs the Rust source graph from Proxmox's Debian Rust source packages and upstream tag metadata.
4. The RPM build runs in a RHEL 9 environment.
5. If no compatibility break exists, the build succeeds without any local metadata edits.
6. If a compatibility break exists, only a narrowly-scoped compatibility patch should be needed.

This plan optimizes for automation and repeatability, not bit-for-bit Debian reproduction.

## Non-Goals

- Exact reproduction of Proxmox's historical Debian `.deb` artifacts.
- Reimplementing Proxmox's full Debian package graph.
- Depending on live `HEAD` of `git://git.proxmox.com/git/proxmox.git` during normal builds.
- Depending on `apt`, `debcargo`, or ad hoc dependency resolution at build time.

## Core Design

The build system should treat the upstream `proxmox-backup` tag as the only human-provided input.

Everything else should be derived automatically:

- the upstream source tree from the tag
- the required Rust crate names and versions from `Cargo.toml`
- the corresponding Debian Rust source packages from the Proxmox archive
- the vendored cargo registry tree from extracted `.deb` payloads
- the upstream git commit provenance for vendored Proxmox crates from `.cargo_vcs_info.json`
- the RPM payload contents from the upstream Debian packaging/install manifests where practical

The build must never resolve against crates.io for Proxmox crates, and should preferably resolve against no network source at all once vendoring is complete.

## Repository Layout To Build Toward

The repository should evolve toward something like:

```text
.
├── PLAN.md
├── proxmox-backup-client.spec
├── patches/
│   └── proxmox-fuse/
│       └── 0001-noflush-compat.patch
├── scripts/
│   ├── bootstrap-tag.sh
│   ├── resolve-crates.py
│   ├── extract-deb-crate.sh
│   ├── vendor-from-archive.py
│   ├── build-rpm.sh
│   ├── latest-upstream-tag.sh
│   └── verify-vendor.py
├── templates/
│   ├── cargo-config.toml.in
│   └── sources.json.schema
├── state/
│   └── ignored-by-git or generated in CI workspace
└── docs/
    └── optional implementation notes
```

Exact filenames can change, but the separation of concerns should remain:

- one bootstrap entrypoint
- one resolver that turns a tag into a vendored registry
- one RPM build entrypoint
- one optional CI helper for latest-tag discovery

## Build Philosophy

The system should behave like a two-phase pipeline.

### Phase 1: Bootstrap

Input:

- upstream `proxmox-backup` tag, for example `v4.1.6`

Output:

- a fully prepared source workspace
- a vendored cargo registry
- an offline `Cargo.lock`
- a manifest describing where each vendored Proxmox crate came from

This phase is allowed to use the network.

### Phase 2: RPM Build

Input:

- the prepared source workspace from phase 1

Output:

- source RPM and binary RPMs

This phase should be offline.

That separation is critical for CI reliability and for understanding failures. If a build fails in phase 2, the issue is in compatibility, packaging, or toolchain behavior, not dependency discovery.

## Source of Truth Rules

The system should follow these precedence rules:

1. `proxmox-backup` tag is the top-level source of truth.
2. The tag's `Cargo.toml` is the source of truth for required crate names and versions.
3. Debian Rust `.deb` packages from the Proxmox archive are the source of truth for vendored Proxmox crate content.
4. `.cargo_vcs_info.json` inside each crate package is the source of truth for the originating git commit/path when present.
5. Upstream Debian packaging files are the source of truth for payload parity where reasonable.
6. Local compatibility patches are allowed only when RHEL-specific or toolchain-specific compatibility demands them.

The build system should avoid any manual crate mapping tables unless a specific upstream inconsistency makes that unavoidable.

## Detailed Workflow

### 1. Accept a tag

The bootstrap command should accept a single required input:

```bash
scripts/bootstrap-tag.sh v4.1.6
```

Everything else should default sensibly.

Optional overrides may include:

- Proxmox archive base URL
- RHEL container image
- output directory
- rust toolchain override
- whether to keep intermediate files

These should be flags, not required configuration edits.

### 2. Fetch upstream `proxmox-backup`

Bootstrap should:

1. clone or fetch `git://git.proxmox.com/git/proxmox-backup.git`
2. check out the requested tag
3. validate that the tag exists
4. record the tag and commit SHA in a generated manifest

Failure here should be fatal.

### 3. Parse the required Rust graph

Bootstrap should inspect the checked-out tag and derive:

- workspace crates
- external crates
- Proxmox crates referenced by version
- feature requirements where relevant

At minimum it should parse:

- root `Cargo.toml`
- workspace member manifests if needed
- `.cargo/config.toml`
- upstream Debian packaging/install files used to infer final outputs

Important constraint:

The parser should not assume that every dependency in `Cargo.toml` must be downloaded as a standalone `.deb` from the Proxmox archive. Some are workspace-local to `proxmox-backup`, some may be provided transitively, and some may come from the distro toolchain if they are not Proxmox-specific.

The resolver should focus first on:

- `proxmox-*`
- `pbs-*`
- `pxar`
- any non-crates.io source that cannot be satisfied generically

### 4. Reconstruct the Proxmox vendored registry

This is the key automation step.

For each required Proxmox crate version:

1. locate the matching `librust-*-dev_<version>*.deb` in the Proxmox archive
2. download the `.deb`
3. extract `usr/share/cargo/registry/<crate>-<version>/`
4. copy that tree into a local vendor registry directory
5. capture provenance:
   - crate name
   - crate version
   - `.deb` filename
   - archive URL
   - `.cargo_vcs_info.json` commit SHA
   - `path_in_vcs`

This step should not require any hand-maintained mapping when new tags appear.

The crate-to-package-name translation should be algorithmic:

- crate name `pbs-api-types` maps to package prefix `librust-pbs-api-types-dev`
- feature subpackages in Debian are metadata, not separate source trees
- version selection comes from the upstream manifest

Where multiple `.deb` revisions exist for the same crate version, prefer the highest Debian revision automatically.

### 5. Verify extracted crate consistency

After vendoring, a verification step should check:

1. every required Proxmox crate version exists in the local vendor registry
2. crate directory names match expected `name-version`
3. `Cargo.toml` inside the vendored crate reports the expected version
4. `.cargo-checksum.json` exists
5. `.cargo_vcs_info.json` exists when present in the package

If a required Proxmox crate cannot be found in the archive, fail fast and report exactly which crate/version is missing.

### 6. Handle non-Proxmox crates

There are two viable models. The implementation should support at least one initially and leave room for the other.

#### Preferred model

Vendor only the Proxmox-specific crates from the Proxmox archive, and resolve the rest from a stable, explicit source during bootstrap, then freeze with `Cargo.lock`.

This likely means:

- use crates.io or a mirrored registry only during bootstrap
- run resolution once
- vendor non-Proxmox crates into the same local vendor tree
- produce a fully offline workspace before the RPM build

Advantages:

- avoids having to mirror Debian's entire Rust package ecosystem
- far easier to automate for future tags
- less coupled to Proxmox archive contents

Tradeoff:

- less faithful than Debian's `/usr/share/cargo/registry` approach for non-Proxmox crates

#### Strict-Debian model

Attempt to source all crates from Debianized Rust packages.

This is not recommended as the default path for automation because it recreates the hardest part of Proxmox's unreproducible environment.

Recommendation:

Start with the preferred model. Treat Proxmox crates as sacred inputs and generic crates as reproducible cargo vendor inputs.

### 7. Generate a local cargo configuration

Bootstrap should generate a workspace-local cargo config that points only at the locally prepared vendor directory.

It should mirror the spirit of upstream:

- no crates.io access during build
- deterministic source replacement

For example:

- local replacement source for all vendored crates
- no dependence on `/usr/share/cargo/registry`

This generated config should live in the prepared build workspace, not as machine-global state.

### 8. Generate and freeze `Cargo.lock`

Once vendoring is complete, bootstrap should generate a `Cargo.lock` in the prepared workspace.

Rules:

- generation must happen after compatibility patches that affect dependencies
- generation must happen using the pinned vendor sources
- the resulting lockfile should be retained as a generated artifact

The repository does not need to commit one static `Cargo.lock` for all tags. Instead, the system should be able to generate the lockfile for any tag automatically.

If later you want fully reviewable releases, CI can attach the generated `Cargo.lock` and vendor manifest as build artifacts.

### 9. Apply compatibility patches automatically

Patches should be optional and data-driven.

The system should support:

- patch directories keyed by crate or package name
- optional metadata describing applicability
- automatic application when the targeted crate is present

For example:

- `patches/proxmox-fuse/0001-noflush-compat.patch`

Recommended policy:

- patch as narrowly as possible
- patch vendored crate source, not unrelated generated files
- make patch application idempotent or fail clearly if already applied

The default case for a new upstream tag should be: no new patch required.

### 10. Build in a RHEL 9 environment

The RPM build should run in a clean RHEL 9 container or chroot, not on a mutable developer host.

The build environment should install only the minimum native requirements:

- RHEL 9 build toolchain
- RPM tooling
- Rust toolchain version selected by policy
- OpenSSL development headers/libs
- fuse3 development headers/libs
- qrencode
- any additional native libraries needed by the resolved tag

The build command should consume the prepared, offline workspace and produce:

- `proxmox-backup-client`
- `pxar`
- matching man pages if built/generated
- shell completions where practical

The target package contents should aim to match upstream Debian client package parity, not the current minimal spec.

### 11. RPM packaging parity

The RPM payload should aim to include at least:

- `/usr/bin/proxmox-backup-client`
- `/usr/bin/pxar`
- man pages for both
- shell completions where available

The spec should derive version information from the upstream tag automatically.

It should not require editing `Version:` manually for each tag.

A practical pattern is:

- pass the upstream tag/version into `rpmbuild` as macros
- point `Source0` at a prepared tarball or workspace archive
- have `%prep`, `%build`, and `%install` operate on the generated workspace

### 12. Record build provenance

Each build should emit a machine-readable manifest containing:

- upstream tag
- upstream commit SHA
- bootstrap timestamp
- crate provenance list
- patch list applied
- Rust toolchain version used
- RHEL build image identifier

This manifest should be shipped as a build artifact and optionally embedded in the SRPM sources.

## Automation Requirements

The system should be designed so that a new tag usually needs no repository edits.

### Required automation properties

- No manual crate version map per tag
- No manual update of spec version fields per tag
- No manual update of helper-crate commit SHAs per tag
- No dependence on `HEAD` of helper repos
- No mutable machine-global cargo config
- No interactive steps

### Acceptable manual intervention

- adding a new compatibility patch when upstream or RHEL changes break compilation
- adjusting native dependency lists when upstream introduces a new system library dependency
- handling genuine upstream archive gaps where a needed crate package was never published

## GitHub Actions Strategy

The CI design should support both scheduled discovery and explicit manual builds.

### Workflow A: Discover Latest Tag

Trigger:

- cron
- manual dispatch

Steps:

1. query upstream `proxmox-backup` tags
2. determine latest semver tag
3. compare against the most recently built tag
4. if unchanged, exit cleanly
5. if new, trigger bootstrap/build workflow

This workflow should not mutate repository metadata just to track the latest tag. Store state in:

- releases
- workflow artifacts
- a lightweight JSON file in a dedicated branch
- or GitHub environment/release metadata

### Workflow B: Build Specific Tag

Trigger:

- workflow dispatch with `tag`
- repository dispatch from workflow A
- push to a branch or tag in this repo if desired

Steps:

1. run bootstrap for the target tag
2. run verification
3. run the RHEL 9 containerized build
4. collect RPMs, SRPM, lockfile, vendor manifest, and logs
5. publish artifacts

### Workflow C: Optional Release Publish

Trigger:

- successful build of a tag

Steps:

1. create or update a GitHub release named after the upstream tag
2. attach RPMs, SRPM, manifest, and lockfile

This is optional but useful because it turns GitHub Releases into the build state store.

## Failure Modes And Expected Responses

### Upstream tag exists but required Proxmox crate package is missing

Response:

- fail bootstrap
- emit a precise error naming the missing crate/version
- do not attempt fallback to helper-repo `HEAD`

Reason:

Falling back to `HEAD` silently destroys fidelity.

### Vendored crate exists but `.cargo_vcs_info.json` is missing

Response:

- warn, but continue if the crate content matches the requested version
- mark provenance as incomplete in the manifest

### New tag compiles on Debian assumptions but fails on RHEL 9

Response:

- add a narrow compatibility patch
- record it under `patches/`
- keep patch applicability automatic

### Proxmox archive layout changes

Response:

- update only the archive discovery code
- keep bootstrap interface unchanged

### Upstream introduces new binaries or changes client packaging contents

Response:

- derive outputs from upstream packaging/install manifests where possible
- avoid hard-coding a static file list beyond the client package scope

## Implementation Order

Build this in stages.

### Stage 1: Prove the bootstrap model

Deliverables:

- fetch a specific tag
- parse required Proxmox crates
- download and extract matching crate `.deb`s
- generate vendor manifest with commit provenance

Success criteria:

- for `v4.1.6`, all required Proxmox crates are resolved automatically

### Stage 2: Produce an offline cargo workspace

Deliverables:

- local cargo config
- combined vendor tree
- generated `Cargo.lock`
- patch application support

Success criteria:

- the workspace can run cargo resolution without network access after bootstrap

### Stage 3: Build the binaries in RHEL 9

Deliverables:

- build script/container definition
- updated RPM spec
- package `proxmox-backup-client` and `pxar`

Success criteria:

- working RPMs for `v4.1.6`

### Stage 4: Automate latest-tag builds

Deliverables:

- GitHub Actions workflows
- artifact publication
- optional release publishing

Success criteria:

- a newly published upstream tag can be built by CI with no repository edits in the common case

## Design Constraints To Enforce During Implementation

- Scripts must be non-interactive.
- Scripts must fail fast with actionable errors.
- Tag input must be the only required human input for normal operation.
- No script should bake in per-tag crate metadata.
- No script should query moving helper branches during normal builds.
- Compatibility patches must stay isolated and reviewable.
- The RPM build phase should be offline.

## Final Recommendation

Implement this repository as a tag-to-vendor-to-RPM pipeline.

The most important architectural decision is:

- derive Proxmox crate source from published Debian Rust source packages
- freeze the resulting workspace locally
- build offline in RHEL 9

That gives the best balance of fidelity, automation, and long-term maintainability.

If this architecture is followed, a future tag like `v4.2.0` should usually require no local changes at all. The normal failure case should be a real compatibility issue, not missing hand-maintained metadata.
