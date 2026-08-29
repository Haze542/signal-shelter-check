#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
    printf 'ERROR: Run as root: sudo ./deploy/backup.sh [backup-directory]\n' >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
BACKUP_DIR="${1:-/var/backups/sheltercheck}"
BACKUP_DIR="$(realpath -m -- "${BACKUP_DIR}")"

case "${BACKUP_DIR}/" in
    "${REPOSITORY_DIR}/"*)
        printf 'ERROR: Refusing to place a production backup inside the Git repository.\n' >&2
        exit 1
        ;;
esac

CONFIG_FILE="/etc/sheltercheck/config.toml"
ROSTER_FILE="/var/lib/sheltercheck/roster.csv"
RELEASED_FILE="/var/lib/sheltercheck/released_today.txt"
STATE_DB="/var/lib/sheltercheck/state.sqlite3"
PYTHON_BIN="/opt/sheltercheck/.venv/bin/python"

for required_file in "${CONFIG_FILE}" "${ROSTER_FILE}" "${STATE_DB}"; do
    if [[ ! -f "${required_file}" ]]; then
        printf 'ERROR: Required backup source is missing: %s\n' "${required_file}" >&2
        exit 1
    fi
done
if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3 || true)"
    if [[ -z "${PYTHON_BIN}" ]]; then
        printf 'ERROR: Python is required for a consistent SQLite backup.\n' >&2
        exit 1
    fi
fi

install -d -o root -g root -m 0700 "${BACKUP_DIR}"
WORK_DIR="$(mktemp -d "${BACKUP_DIR}/.sheltercheck-backup.XXXXXX")"
cleanup() {
    rm -r -- "${WORK_DIR}"
}
trap cleanup EXIT

install -D -o root -g root -m 0600 \
    "${CONFIG_FILE}" "${WORK_DIR}/etc/sheltercheck/config.toml"
install -D -o root -g root -m 0600 \
    "${ROSTER_FILE}" "${WORK_DIR}/var/lib/sheltercheck/roster.csv"
if [[ -f "${RELEASED_FILE}" ]]; then
    install -D -o root -g root -m 0600 \
        "${RELEASED_FILE}" "${WORK_DIR}/var/lib/sheltercheck/released_today.txt"
fi

mkdir -p "${WORK_DIR}/var/lib/sheltercheck"
"${PYTHON_BIN}" - "${STATE_DB}" "${WORK_DIR}/var/lib/sheltercheck/state.sqlite3" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
    result = destination.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        raise SystemExit("SQLite backup quick_check failed")
finally:
    destination.close()
    source.close()
PY
chmod 0600 "${WORK_DIR}/var/lib/sheltercheck/state.sqlite3"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${BACKUP_DIR}/sheltercheck-${TIMESTAMP}-$$.tar.gz"
tar -C "${WORK_DIR}" -czf "${ARCHIVE}" etc var
chmod 0600 "${ARCHIVE}"

printf 'Backup created: %s\n' "${ARCHIVE}"
printf 'Signal account state was NOT included. Keep any separate Signal-state backup encrypted and access-restricted.\n'
