#!/usr/bin/env bash
set -euo pipefail

service="${K2_OPENHOST_KLIPPER_SERVICE:-klipper.service}"
dropin_dir="/etc/systemd/system/${service}.d"
helper="/usr/local/libexec/k2-openhost/wait-transport.sh"
dropin="${dropin_dir}/k2-openhost-transport.conf"

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo: sudo $0 [--remove]" >&2
    exit 2
fi

if [[ "${1:-}" == "--remove" ]]; then
    rm -f "${dropin}" "${helper}"
    systemctl daemon-reload
    echo "Removed K2-OpenHost transport gate from ${service}."
    exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -Dm755 "${repo_root}/scripts/k2-openhost-wait-transport.sh" "${helper}"
mkdir -p "${dropin_dir}"
cat > "${dropin}" <<'DROPIN'
[Service]
Environment="K2_OPENHOST_TRANSPORT_TIMEOUT=60"
Environment="K2_OPENHOST_TRANSPORT_DEVICES=/dev/serial/by-id/usb-Allwinner_Technology_Inc._Gadget_Serial-if00-port0 /dev/serial/by-id/usb-Allwinner_Technology_Inc._Gadget_Serial-if01-port0 /dev/serial/by-id/usb-Allwinner_Technology_Inc._Gadget_Serial-if02-port0"
ExecStartPre=/usr/local/libexec/k2-openhost/wait-transport.sh
Restart=on-failure
RestartSec=5
DROPIN
systemctl daemon-reload
echo "Installed ${dropin}. Restart Klipper when ready: systemctl restart ${service}"
