#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Installer for wayport (host CLI and optional guest pure-VSOCK daemon).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.local/bin"
SCRIPT_NAME="wayport"
ACTION="host"

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --host        Install host CLI into ${INSTALL_DIR} (default)
  --guest       Install inside a guest VM and enable wayport-guest.service (pure VSOCK mode)
  --uninstall   Remove wayport binaries, systemd units, exported .desktop launchers, icons, and cache
  -h, --help    Show this help message
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --host)
            ACTION="host"
            shift
            ;;
        --guest)
            ACTION="guest"
            shift
            ;;
        --uninstall)
            ACTION="uninstall"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [ "$ACTION" = "uninstall" ]; then
    echo "[*] Uninstalling ${SCRIPT_NAME} and cleaning up generated files..."

    SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
    HAD_UNITS=0
    HAD_GUEST_UNIT=0
    for unit_path in "${SYSTEMD_USER_DIR}"/wayport-tray-*.service "${SYSTEMD_USER_DIR}/wayport-guest.service"; do
        [ -e "$unit_path" ] || continue
        unit_name="$(basename "$unit_path")"
        if [ "$unit_name" = "wayport-guest.service" ]; then
            HAD_GUEST_UNIT=1
        fi
        if command -v systemctl >/dev/null 2>&1; then
            systemctl --user disable --now "$unit_name" >/dev/null 2>&1 || true
        fi
        rm -f "$unit_path"
        echo "[OK] Removed systemd unit: ${unit_name}"
        HAD_UNITS=1
    done
    if [ "$HAD_UNITS" -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user reset-failed >/dev/null 2>&1 || true
    fi

    for bin_name in "${SCRIPT_NAME}" wayport-wrapper wayport-tray-agent wayport-daemon; do
        if [ -e "${INSTALL_DIR}/${bin_name}" ] || [ -L "${INSTALL_DIR}/${bin_name}" ]; then
            rm -f "${INSTALL_DIR}/${bin_name}"
            echo "[OK] Removed ${INSTALL_DIR}/${bin_name}"
        fi
    done

    APPS_DIR="${HOME}/.local/share/applications"
    REMOVED_DESKTOPS=0
    for desktop_file in "${APPS_DIR}"/wayport-*.desktop; do
        [ -e "$desktop_file" ] || continue
        rm -f "$desktop_file"
        REMOVED_DESKTOPS=$((REMOVED_DESKTOPS + 1))
    done
    if [ "$REMOVED_DESKTOPS" -gt 0 ]; then
        echo "[OK] Removed ${REMOVED_DESKTOPS} exported .desktop launcher(s) from ${APPS_DIR}"
    fi

    ICONS_DIR="${HOME}/.local/share/icons/wayport"
    if [ -d "$ICONS_DIR" ]; then
        rm -rf "$ICONS_DIR"
        echo "[OK] Removed ${ICONS_DIR}"
    fi

    CACHE_DIR="${HOME}/.cache/wayport"
    if [ -d "$CACHE_DIR" ]; then
        rm -rf "$CACHE_DIR"
        echo "[OK] Removed ${CACHE_DIR}"
    fi

    RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    for rt_file in "${RUNTIME_DIR}"/wayport-*.sock "${RUNTIME_DIR}"/wayport-*.stamp; do
        [ -e "$rt_file" ] || [ -S "$rt_file" ] || continue
        rm -f "$rt_file"
    done

    if [ "$REMOVED_DESKTOPS" -gt 0 ]; then
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
        fi
        if command -v kbuildsycoca6 >/dev/null 2>&1; then
            kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
        fi
    fi

    if [ "$HAD_GUEST_UNIT" -eq 1 ]; then
        echo "[i] Note: systemd user linger was left unchanged. To disable it: loginctl disable-linger \"\$USER\""
    fi

    echo "[DONE] ${SCRIPT_NAME} uninstall complete."
    exit 0
fi

SOURCE_SCRIPT="${SCRIPT_DIR}/${SCRIPT_NAME}"
if [ ! -f "$SOURCE_SCRIPT" ]; then
    echo "[ERROR] Cannot find ${SOURCE_SCRIPT}" >&2
    exit 1
fi

echo "[*] Installing ${SCRIPT_NAME} to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
install -m 0755 "${SOURCE_SCRIPT}" "${INSTALL_DIR}/${SCRIPT_NAME}"

if ! command -v waypipe >/dev/null 2>&1; then
    echo "[!] Warning: 'waypipe' is not installed. Install it via: sudo apt install waypipe"
fi

if [ "$ACTION" = "guest" ]; then
    if [ ! -e /dev/vsock ]; then
        echo "[!] Warning: /dev/vsock not found inside this VM. Attach a Virtio VSOCK device for pure vsock mode."
    fi
    "${INSTALL_DIR}/${SCRIPT_NAME}" install-guest --local
    echo "[OK] Guest setup complete."
    exit 0
fi

if [ ! -e /dev/vhost-vsock ]; then
    echo "[!] Note: /dev/vhost-vsock not found (needed for 'hybrid' and 'vsock' modes; not required for 'ssh' mode)."
    echo "          To enable VSOCK on the host: sudo modprobe vhost_vsock"
fi

if [[ ":${PATH}:" != *":${INSTALL_DIR}:"* ]]; then
    echo "[!] Note: ${INSTALL_DIR} is not currently in your PATH."
fi

echo "[OK] Installed ${INSTALL_DIR}/${SCRIPT_NAME}"
echo "     Run '${SCRIPT_NAME} doctor' or '${SCRIPT_NAME} --help' to get started."
