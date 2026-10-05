#!/usr/bin/env bash
set -euo pipefail

timeout="${K2_OPENHOST_TRANSPORT_TIMEOUT:-60}"
# The T113 gadget channels by interface (if00 Main, if01 Nozzle, if02 RS-485),
# the names printer.cfg uses: /dev/ttyUSBn numbers follow enumeration order.
devices="${K2_OPENHOST_TRANSPORT_DEVICES:-/dev/serial/by-id/usb-Allwinner_Technology_Inc._Gadget_Serial-if00-port0 /dev/serial/by-id/usb-Allwinner_Technology_Inc._Gadget_Serial-if01-port0 /dev/serial/by-id/usb-Allwinner_Technology_Inc._Gadget_Serial-if02-port0}"
started=$SECONDS

echo "k2-openhost: waiting up to ${timeout}s for transport: ${devices}" >&2
while (( SECONDS - started < timeout )); do
    missing=()
    for dev in ${devices}; do
        if [[ ! -c "${dev}" || ! -r "${dev}" || ! -w "${dev}" ]]; then
            missing+=("${dev}")
        fi
    done
    if (( ${#missing[@]} == 0 )); then
        echo "k2-openhost: transport ready after $((SECONDS-started))s" >&2
        exit 0
    fi
    sleep 0.25
done

echo "k2-openhost: transport timeout; unavailable devices:" >&2
for dev in ${devices}; do
    [[ -c "${dev}" && -r "${dev}" && -w "${dev}" ]] || echo "  ${dev}" >&2
done
exit 1
