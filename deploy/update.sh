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

"${SCRIPT_DIR}/install.sh"

runuser -u sheltercheck -- /opt/sheltercheck/.venv/bin/python -m sheltercheck \
    --config /etc/sheltercheck/config.toml \
    --validate-config

if [[ ${WAS_ACTIVE} -eq 1 ]]; then
    systemctl restart sheltercheck.service
    printf '\nShelterCheck was updated and restarted.\n'
else
    printf '\nShelterCheck was updated. It was not running, so it remains stopped.\n'
fi
