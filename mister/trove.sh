#!/usr/bin/env bash
# Trove — interactive menu wrapper for MiSTer's Scripts menu.
#
# Launched with a controller from MiSTer's script picker. Wraps the CLI in a
# whiptail TUI so users don't need to SSH for common actions. All actions
# route back to `trove <cmd>` for the actual work.

set -eu

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Resolve installed root (this script may be symlinked from Scripts/trove.sh)
if [ -L "${BASH_SOURCE[0]}" ]; then
    TARGET="$(readlink "${BASH_SOURCE[0]}")"
    case "$TARGET" in
        /*) HERE="$(dirname "$TARGET")" ;;
        *)  HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/$(dirname "$TARGET")" && pwd)" ;;
    esac
fi
ROOT="$(dirname "$HERE")"
TROVE="$ROOT/bin/trove"

command -v whiptail >/dev/null 2>&1 || { echo "whiptail not installed"; exit 1; }
[ -x "$TROVE" ] || { echo "trove not found at $TROVE"; exit 1; }

TITLE="Trove — RomM ↔ MiSTer"

pause() {
    echo
    read -r -p "Press Enter to continue…" _
}

while true; do
    CHOICE=$(whiptail --title "$TITLE" --menu "Choose an action" 22 78 13 \
        "status"       "Show install + subscription summary" \
        "pair"         "Pair with a RomM server (60s code)" \
        "sync-dry"     "Preview sync (dry-run, no changes)" \
        "sync"         "Run sync now" \
        "bios-all"     "Download BIOS for all subscribed platforms" \
        "config"       "Edit config.json in vi" \
        "doctor"       "Sanity-check the install" \
        "enable-cron"  "Start crond + persist at boot (for auto-sync)" \
        "update"       "Check for updates / apply" \
        "logs"         "Tail the latest log" \
        "exit"         "Exit" \
        3>&1 1>&2 2>&3) || exit 0

    clear
    case "$CHOICE" in
        status)       "$TROVE" status; pause ;;
        pair)         "$TROVE" pair; pause ;;
        sync-dry)     "$TROVE" sync --dry-run; pause ;;
        sync)         "$TROVE" sync; pause ;;
        bios-all)     "$TROVE" bios all; pause ;;
        config)       "$TROVE" config; pause ;;
        doctor)       "$TROVE" doctor; pause ;;
        enable-cron)  "$TROVE" enable-cron; pause ;;
        update)       "$TROVE" update; pause ;;
        logs)         tail -n 50 "$ROOT/logs/trove.log" 2>/dev/null || echo "no log yet"; pause ;;
        exit|"")      exit 0 ;;
    esac
done
