#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="sheltercheck"
SERVICE_GROUP="sheltercheck"
APP_DIR="/opt/sheltercheck"
CONFIG_DIR="/etc/sheltercheck"
STATE_DIR="/var/lib/sheltercheck"
SIGNAL_STATE_DIR="${STATE_DIR}/signal-cli"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

if [[ ${EUID} -ne 0 ]]; then
    fail "Run this installer as root: sudo ./deploy/install.sh"
fi

[[ "$(uname -s)" == "Linux" ]] || fail "This installer supports Linux only."

case "$(uname -m)" in
    x86_64|amd64)
        ARCH="x86_64"
        ;;
    aarch64|arm64)
        ARCH="arm64"
        ;;
    *)
        fail "Unsupported architecture: $(uname -m). Supported: x86_64 and arm64."
        ;;
esac

command -v systemctl >/dev/null 2>&1 || fail "systemd/systemctl is required."
command -v useradd >/dev/null 2>&1 || fail "useradd is required."
command -v groupadd >/dev/null 2>&1 || fail "groupadd is required."
command -v python3 >/dev/null 2>&1 || fail "Python 3.12 or newer is required."

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    fail "Python 3.12 or newer is required. Install a supported Python and python3-venv, then rerun."
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
    fail "Python venv support is missing. On Debian/Ubuntu install python3-venv; on Arch install the python package."
fi

SIGNAL_CLI_BIN="$(command -v signal-cli || true)"
if [[ -z "${SIGNAL_CLI_BIN}" ]]; then
    fail "signal-cli is not installed. Install a current upstream signal-cli build for ${ARCH}, verify 'signal-cli --version', then rerun. See README.md."
fi
SIGNAL_CLI_BIN="$(readlink -f -- "${SIGNAL_CLI_BIN}")"
[[ -x "${SIGNAL_CLI_BIN}" ]] || fail "Resolved signal-cli is not executable: ${SIGNAL_CLI_BIN}"
case "${SIGNAL_CLI_BIN}" in
    /home/*|/root/*)
        fail "signal-cli must be installed system-wide, not under a user's home: ${SIGNAL_CLI_BIN}"
        ;;
esac
SIGNAL_CLI_VERSION="$("${SIGNAL_CLI_BIN}" --version 2>&1 | head -n 1)"
[[ -n "${SIGNAL_CLI_VERSION}" ]] || fail "signal-cli exists but its version could not be read."
"${SIGNAL_CLI_BIN}" daemon --help 2>&1 | grep -q -- '--http' \
    || fail "Installed signal-cli does not support the required daemon --http option."

NOLOGIN_SHELL="$(command -v nologin || true)"
[[ -n "${NOLOGIN_SHELL}" ]] || NOLOGIN_SHELL="/usr/sbin/nologin"

if ! getent group "${SERVICE_GROUP}" >/dev/null; then
    groupadd --system "${SERVICE_GROUP}"
fi
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd \
        --system \
        --gid "${SERVICE_GROUP}" \
        --home-dir "${STATE_DIR}" \
        --no-create-home \
        --shell "${NOLOGIN_SHELL}" \
        "${SERVICE_USER}"
else
    usermod \
        --gid "${SERVICE_GROUP}" \
        --home "${STATE_DIR}" \
        --shell "${NOLOGIN_SHELL}" \
        "${SERVICE_USER}"
fi

for managed_dir in "${APP_DIR}" "${CONFIG_DIR}" "${STATE_DIR}" "${SIGNAL_STATE_DIR}"; do
    [[ ! -L "${managed_dir}" ]] || fail "Refusing to manage symlinked directory: ${managed_dir}"
done

install -d -o root -g root -m 0755 "${APP_DIR}"
install -d -o root -g "${SERVICE_GROUP}" -m 0750 "${CONFIG_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0750 "${STATE_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0700 "${SIGNAL_STATE_DIR}"

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    python3 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --upgrade \
    "${SOURCE_DIR}"
"${APP_DIR}/.venv/bin/python" -c 'import sheltercheck'
ln -sfn -- "${SIGNAL_CLI_BIN}" "${APP_DIR}/signal-cli"

if [[ -L "${CONFIG_FILE}" ]]; then
    fail "Refusing to manage symlinked production config: ${CONFIG_FILE}"
elif [[ ! -e "${CONFIG_FILE}" ]]; then
    install -o root -g "${SERVICE_GROUP}" -m 0640 \
        "${SOURCE_DIR}/config.example.toml" "${CONFIG_FILE}"
elif [[ ! -f "${CONFIG_FILE}" ]]; then
    fail "Production config exists but is not a regular file: ${CONFIG_FILE}"
fi
chown root:"${SERVICE_GROUP}" "${CONFIG_FILE}"
chmod 0640 "${CONFIG_FILE}"

ROSTER_FILE="${STATE_DIR}/roster.csv"
RELEASED_FILE="${STATE_DIR}/released_today.txt"
if [[ -L "${ROSTER_FILE}" ]]; then
    fail "Refusing to manage symlinked roster: ${ROSTER_FILE}"
elif [[ ! -e "${ROSTER_FILE}" ]]; then
    install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0600 \
        "${SOURCE_DIR}/examples/roster.example.csv" "${ROSTER_FILE}"
elif [[ ! -f "${ROSTER_FILE}" ]]; then
    fail "Roster exists but is not a regular file: ${ROSTER_FILE}"
fi
if [[ -L "${RELEASED_FILE}" ]]; then
    fail "Refusing to manage symlinked released list: ${RELEASED_FILE}"
elif [[ ! -e "${RELEASED_FILE}" ]]; then
    install -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" -m 0600 \
        "${SOURCE_DIR}/examples/released_today.example.txt" "${RELEASED_FILE}"
elif [[ ! -f "${RELEASED_FILE}" ]]; then
    fail "Released list exists but is not a regular file: ${RELEASED_FILE}"
fi
chown "${SERVICE_USER}:${SERVICE_GROUP}" "${ROSTER_FILE}" "${RELEASED_FILE}"
chmod 0600 "${ROSTER_FILE}" "${RELEASED_FILE}"

for database_file in \
    "${STATE_DIR}/state.sqlite3" \
    "${STATE_DIR}/state.sqlite3-wal" \
    "${STATE_DIR}/state.sqlite3-shm"; do
    if [[ -e "${database_file}" ]]; then
        [[ ! -L "${database_file}" ]] || fail "Refusing to manage symlinked SQLite file: ${database_file}"
        chown "${SERVICE_USER}:${SERVICE_GROUP}" "${database_file}"
        chmod 0600 "${database_file}"
    fi
done
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${SIGNAL_STATE_DIR}"
find "${SIGNAL_STATE_DIR}" -type d -exec chmod 0700 {} +
find "${SIGNAL_STATE_DIR}" -type f -exec chmod 0600 {} +

install -o root -g root -m 0644 \
    "${SOURCE_DIR}/deploy/signal-cli.service" /etc/systemd/system/signal-cli.service
install -o root -g root -m 0644 \
    "${SOURCE_DIR}/deploy/sheltercheck.service" /etc/systemd/system/sheltercheck.service
systemctl daemon-reload

printf '\nInstallation complete.\n'
printf 'Architecture: %s\n' "${ARCH}"
printf 'Detected: %s\n\n' "${SIGNAL_CLI_VERSION}"
cat <<'EOF'
NEXT STEPS:

1. Link signal-cli (do not start the services yet):
   sudo -u sheltercheck /opt/sheltercheck/signal-cli --data-dir /var/lib/sheltercheck/signal-cli link -n "ShelterCheck server"
   Scan the shown QR code in Signal: Settings -> Linked devices -> Link new device.

2. Edit configuration:
   sudo nano /etc/sheltercheck/config.toml

3. Edit roster:
   sudo nano /var/lib/sheltercheck/roster.csv

4. Edit today's released list:
   sudo nano /var/lib/sheltercheck/released_today.txt

5. Validate:
   sudo -u sheltercheck /opt/sheltercheck/.venv/bin/python -m sheltercheck --config /etc/sheltercheck/config.toml --validate-config

6. Start at boot and now:
   sudo systemctl enable --now signal-cli.service
   sudo systemctl enable --now sheltercheck.service

7. Check:
   systemctl status signal-cli.service --no-pager
   systemctl status sheltercheck.service --no-pager
   sudo -u sheltercheck /opt/sheltercheck/.venv/bin/python -m sheltercheck --config /etc/sheltercheck/config.toml --health
EOF
