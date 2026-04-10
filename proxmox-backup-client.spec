Name:           proxmox-backup-client
Version:        %{pkg_version}
Release:        1%{?dist}
Summary:        Client for Proxmox Backup Server
License:        AGPL-3.0-only
URL:            https://www.proxmox.com/en/proxmox-backup-server
ExclusiveArch:  x86_64

# qrencode (CLI tool) is called as a subprocess by proxmox-backup-client
# to display encryption keys as QR codes.  Shared-library Requires entries
# (fuse3-libs, openssl-libs, etc.) are generated automatically by rpmbuild
# from the ELF binary via AutoReq.
Requires:       qrencode

%description
proxmox-backup-client is a command-line tool for backing up and restoring
data to/from a Proxmox Backup Server over a TLS-encrypted connection.

%install
install -Dm755 %{binary_path} %{buildroot}%{_bindir}/proxmox-backup-client

%files
%{_bindir}/proxmox-backup-client

%changelog
