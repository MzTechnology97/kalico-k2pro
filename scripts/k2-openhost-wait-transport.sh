#!/usr/bin/env bash
set -euo pipefail

timeout="${K2_OPENHOST_TRANSPORT_TIMEOUT:-60}"
devices="${K2_OPENHOST_TRANSPORT_DEVICES:-/dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyUSB2}"
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
