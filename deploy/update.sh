#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    printf 'ERROR: Run as root: sudo ./deploy/update.sh\n' >&2
    exit 1
fi
command -v runuser >/dev/null 2>&1 || {
    printf 'ERROR: runuser is required to validate as the sheltercheck user.\n' >&2
    exit 1
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WAS_ACTIVE=0
if systemctl is-active --quiet sheltercheck.service; then
    WAS_ACTIVE=1
fi
SIGNAL_WAS_ACTIVE=0
if systemctl is-active --quiet signal-cli.service; then
    SIGNAL_WAS_ACTIVE=1
fi

"${SCRIPT_DIR}/install.sh"

runuser -u sheltercheck -- /opt/sheltercheck/.venv/bin/python -m sheltercheck \
    --config /etc/sheltercheck/config.toml \
    --validate-config

if [[ ${SIGNAL_WAS_ACTIVE} -eq 1 ]]; then
    if ! systemctl restart signal-cli.service; then
        printf '\nsignal-cli failed its first readiness check; waiting for automatic recovery.\n'
    fi
    runuser -u sheltercheck -- \
        /opt/sheltercheck/.venv/bin/python \
        /opt/sheltercheck/signal_cli_readiness.py \
            --url http://127.0.0.1:8080 \
            --timeout-seconds 180 \
            --wait-for-account
fi

if [[ ${WAS_ACTIVE} -eq 1 ]]; then
    systemctl restart sheltercheck.service
    printf '\nShelterCheck was updated and restarted.\n'
else
    printf '\nShelterCheck was updated. It was not running, so it remains stopped.\n'
fi
