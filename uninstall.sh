#!/usr/bin/env bash
# Trove uninstaller.
#
# Removes the install directory, the launcher symlink, and the cron entry.
# By default, preserves config.json and manifest.json so a re-install picks
# them up. Pass --purge to remove those too.

set -eu

TROVE_HOME="${TROVE_HOME:-/media/fat/Scripts/.trove}"
SCRIPTS_DIR="/media/fat/Scripts"
LAUNCHER_NAME="trove.sh"
PURGE=0

log()  { printf '\033[36m[trove]\033[0m %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        --home)  TROVE_HOME="${2:-}"; shift 2 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'unknown flag: %s\n' "$1" >&2; exit 2 ;;
    esac
done

log "removing launcher symlink"
rm -f "$SCRIPTS_DIR/$LAUNCHER_NAME"

log "removing cron entry"
rm -f /etc/cron.d/trove
# Legacy: also clean out anything we may have added to the root crontab.
if command -v crontab >/dev/null 2>&1; then
    (crontab -l 2>/dev/null | grep -v '# TROVE') 2>/dev/null | crontab - 2>/dev/null || true
fi

if [ "$PURGE" -eq 1 ]; then
    log "purging install dir + config: $TROVE_HOME"
    rm -rf "$TROVE_HOME"
else
    log "removing install dir (preserving config.json + manifest.json + logs)"
    if [ -d "$TROVE_HOME" ]; then
        find "$TROVE_HOME" -mindepth 1 -maxdepth 1 ! -name 'config.json' ! -name 'manifest.json' ! -name 'logs' -exec rm -rf {} +
    fi
fi

log "uninstall complete."
