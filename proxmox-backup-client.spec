%global prepared_name proxmox-backup-prepared
%global debug_package %{nil}
%global _debugsource_packages 0

Name:           proxmox-backup-client
Version:        4.1.5
Release:        1%{?dist}
Summary:        Client for Proxmox Backup Server
License:        AGPL-3.0-only
URL:            https://www.proxmox.com/en/proxmox-backup-server
ExclusiveArch:  x86_64
Source0:        %{prepared_name}-%{version}.tar.gz

BuildRequires:  bash
BuildRequires:  cargo
BuildRequires:  gcc
BuildRequires:  rust
BuildRequires:  pkgconfig(fuse3)
BuildRequires:  pkgconfig(libacl)
BuildRequires:  pkgconfig(libsystemd)
BuildRequires:  pkgconfig(libudev)
BuildRequires:  pkgconfig(libzstd)
BuildRequires:  pkgconfig(openssl)
BuildRequires:  pkgconfig(uuid)

# qrencode (CLI tool) is called as a subprocess by proxmox-backup-client
# to display encryption keys as QR codes.  Shared-library Requires entries
# (fuse3-libs, openssl-libs, etc.) are generated automatically by rpmbuild
# from the ELF binary via AutoReq.
Requires:       qrencode

%description
proxmox-backup-client is a command-line tool for backing up and restoring
data to/from a Proxmox Backup Server over a TLS-encrypted connection.

%prep
%setup -q -n %{prepared_name}-%{version}

%build
cargo build --manifest-path Cargo.toml --release --locked \
    --package proxmox-backup-client --bin proxmox-backup-client \
    --package pxar-bin --bin pxar

%install
install -Dm755 target/release/proxmox-backup-client %{buildroot}%{_bindir}/proxmox-backup-client
install -Dm755 target/release/pxar %{buildroot}%{_bindir}/pxar
install -Dm644 debian/proxmox-backup-client.bc \
    %{buildroot}%{_datadir}/bash-completion/completions/proxmox-backup-client
install -Dm644 debian/pxar.bc \
    %{buildroot}%{_datadir}/bash-completion/completions/pxar
install -Dm644 zsh-completions/_proxmox-backup-client \
    %{buildroot}%{_datadir}/zsh/vendor-completions/_proxmox-backup-client
install -Dm644 zsh-completions/_pxar \
    %{buildroot}%{_datadir}/zsh/vendor-completions/_pxar
install -Dm644 debian/copyright \
    %{buildroot}%{_licensedir}/%{name}/copyright

%files
%license %{_licensedir}/%{name}/copyright
%doc .rpm-metadata
%{_bindir}/proxmox-backup-client
%{_bindir}/pxar
%{_datadir}/bash-completion/completions/proxmox-backup-client
%{_datadir}/bash-completion/completions/pxar
%{_datadir}/zsh/vendor-completions/_proxmox-backup-client
%{_datadir}/zsh/vendor-completions/_pxar

%changelog
