#!/usr/bin/env bash
# Trove installer — headless MiSTer setup.
#
# Fetches the latest Trove release, unpacks to /media/fat/Scripts/.trove/,
# symlinks the launcher into /media/fat/Scripts/, and (optionally) adds a cron
# entry. Requires only Python 3 (already on standard MiSTer builds), curl or
# wget, and tar.
#
# Usage:
#   curl -kL https://raw.githubusercontent.com/borger/trove/main/install.sh | bash
# or:
#   bash install.sh --dev --source /path/to/local/checkout
#
# Environment variables:
#   TROVE_HOME    — install directory (default: /media/fat/Scripts/.trove)
#   TROVE_REPO    — GitHub repo (default: borger/trove)
#   TROVE_TAG     — release tag to install (default: latest)

set -euo pipefail

TROVE_HOME="${TROVE_HOME:-/media/fat/Scripts/.trove}"
TROVE_REPO="${TROVE_REPO:-borger/trove}"
TROVE_TAG="${TROVE_TAG:-latest}"
SCRIPTS_DIR="/media/fat/Scripts"
LAUNCHER_NAME="trove.sh"
DEV_SOURCE=""
NO_CRON=0
ENABLE_CROND=0

log()  { printf '\033[36m[trove]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[trove]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[trove]\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dev)           DEV_SOURCE="${2:-}"; shift 2 ;;
        --source)        DEV_SOURCE="${2:-}"; shift 2 ;;
        --no-cron)       NO_CRON=1; shift ;;
        --enable-cron)   ENABLE_CROND=1; shift ;;
        --home)          TROVE_HOME="${2:-}"; shift 2 ;;
        --tag)           TROVE_TAG="${2:-}"; shift 2 ;;
        -h|--help)       grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)               die "unknown flag: $1" ;;
    esac
done

log "install target: $TROVE_HOME"

# ── prerequisites ─────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH — Trove requires it (standard on current MiSTer builds via update_all)."
command -v tar >/dev/null 2>&1 || die "tar not found"
if command -v curl >/dev/null 2>&1; then
    FETCH="curl -kL --fail --progress-bar"
elif command -v wget >/dev/null 2>&1; then
    FETCH="wget -q --show-progress -O -"
else
    die "curl or wget required"
fi

PYTHON_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
log "python3 detected: $PYTHON_VERSION"

# ── fetch source ──────────────────────────────────────────────────────────
mkdir -p "$TROVE_HOME"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

if [ -n "$DEV_SOURCE" ]; then
    log "dev install from local source: $DEV_SOURCE"
    cp -a "$DEV_SOURCE"/. "$STAGING/"
else
    if [ "$TROVE_TAG" = "latest" ]; then
        log "resolving latest release tag from GitHub…"
        TROVE_TAG="$($FETCH "https://api.github.com/repos/$TROVE_REPO/releases/latest" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("tag_name", ""))')"
        [ -n "$TROVE_TAG" ] || die "could not resolve latest release tag (repo empty? no releases yet?)"
    fi
    log "downloading Trove $TROVE_TAG…"
    $FETCH "https://github.com/$TROVE_REPO/archive/refs/tags/$TROVE_TAG.tar.gz" > "$STAGING/trove.tar.gz"
    tar -xzf "$STAGING/trove.tar.gz" -C "$STAGING"
    rm "$STAGING/trove.tar.gz"
    # tarball wraps everything in one top-level dir — flatten it
    TOP="$(find "$STAGING" -mindepth 1 -maxdepth 1 -type d | head -1)"
    mv "$TOP"/* "$STAGING/" 2>/dev/null || true
    mv "$TOP"/.* "$STAGING/" 2>/dev/null || true
    rmdir "$TOP" 2>/dev/null || true
fi

# ── install (preserve config + manifest + logs across upgrades) ───────────
PRESERVE=("config.json" "manifest.json" "logs")
if [ -d "$TROVE_HOME" ]; then
    log "preserving existing config/manifest/logs (if present)…"
    for p in "${PRESERVE[@]}"; do
        if [ -e "$TROVE_HOME/$p" ]; then
            cp -a "$TROVE_HOME/$p" "$STAGING/.preserve-$p"
        fi
    done
fi

log "wiping $TROVE_HOME (config preserved)…"
find "$TROVE_HOME" -mindepth 1 -maxdepth 1 ! -name 'config.json' ! -name 'manifest.json' ! -name 'logs' -exec rm -rf {} +

log "installing files → $TROVE_HOME"
for item in "$STAGING"/*; do
    [ -e "$item" ] || continue
    name="$(basename "$item")"
    case "$name" in
        .preserve-*) ;;
        *) cp -a "$item" "$TROVE_HOME/" ;;
    esac
done

# Restore preserved state (only if not carried over intact)
for p in "${PRESERVE[@]}"; do
    if [ ! -e "$TROVE_HOME/$p" ] && [ -e "$STAGING/.preserve-$p" ]; then
        cp -a "$STAGING/.preserve-$p" "$TROVE_HOME/$p"
    fi
done

chmod +x "$TROVE_HOME/bin/trove" 2>/dev/null || true
chmod +x "$TROVE_HOME/mister/trove.sh" 2>/dev/null || true

# ── launcher visible in MiSTer's Scripts menu ─────────────────────────────
if [ -d "$SCRIPTS_DIR" ]; then
    log "installing launcher → $SCRIPTS_DIR/$LAUNCHER_NAME"
    ln -sf "$TROVE_HOME/mister/trove.sh" "$SCRIPTS_DIR/$LAUNCHER_NAME"
fi

# ── cron entry ────────────────────────────────────────────────────────────
# MiSTer's stock userland ships /usr/sbin/crond as a symlink to BusyBox.
# BusyBox crond reads /var/spool/cron/crontabs/<user> — a single file per
# user, entries WITHOUT the user field (the filename IS the user). This is
# different from Debian's cron.d/ drop-in format.
#
# For safety we install BOTH: the BusyBox-native /var/spool/cron/crontabs/root
# (which is what MiSTer needs) AND the /etc/cron.d/trove drop-in (which works
# on any non-BusyBox setup or if a user later swaps the cron implementation).

crond_is_running() {
    if command -v pgrep >/dev/null 2>&1; then
        pgrep crond >/dev/null 2>&1
    else
        ps -ef 2>/dev/null | grep -v grep | grep -q crond
    fi
}

install_cron_entry() {
    local schedule cmd busy_dir busy_file cron_d_file existing
    schedule="$(python3 -c "import json,sys,os; p='$TROVE_HOME/config.json'; c=json.load(open(p)) if os.path.exists(p) else {}; print(((c.get('cron') or {}).get('schedule')) or '0 3 * * *')" 2>/dev/null || echo "0 3 * * *")"
    cmd="$TROVE_HOME/bin/trove sync --quiet"

    # 1) BusyBox-native (MiSTer's default) — /var/spool/cron/crontabs/root
    busy_dir="/var/spool/cron/crontabs"
    busy_file="$busy_dir/root"
    mkdir -p "$busy_dir"
    existing=""
    [ -f "$busy_file" ] && existing="$(grep -v '# TROVE' "$busy_file" 2>/dev/null || true)"
    {
        [ -n "$existing" ] && printf '%s\n' "$existing"
        printf '%s  %s  # TROVE\n' "$schedule" "$cmd"
    } > "$busy_file"
    chmod 600 "$busy_file"
    log "cron entry installed: $busy_file ($schedule)  [BusyBox crond]"

    # 2) Debian-style drop-in — /etc/cron.d/trove (harmless on BusyBox systems)
    cron_d_file="/etc/cron.d/trove"
    if [ -w /etc/cron.d ] || [ -w /etc ] 2>/dev/null; then
        printf '# Installed by Trove — remove with trove uninstall.\n%s  root  %s\n' "$schedule" "$cmd" > "$cron_d_file" 2>/dev/null || true
        [ -f "$cron_d_file" ] && chmod 644 "$cron_d_file" 2>/dev/null || true
    fi
}

enable_crond_at_boot() {
    local startup="/media/fat/linux/user-startup.sh"
    local line="/usr/sbin/crond -b"
    if [ -f "$startup" ] && grep -qF "$line" "$startup" 2>/dev/null; then
        log "crond already configured to start at boot in $startup"
        return
    fi
    mkdir -p "$(dirname "$startup")"
    {
        [ ! -f "$startup" ] && printf '#!/bin/bash\n# MiSTer user startup — commands added by installers.\n\n'
        printf '# Added by Trove installer — start BusyBox crond so /etc/cron.d and /var/spool/cron/crontabs/root fire.\n%s\n' "$line"
    } >> "$startup"
    chmod +x "$startup"
    log "added '$line' to $startup — will start at next boot"
}

start_crond_now() {
    if crond_is_running; then
        log "crond is already running"
        return
    fi
    /usr/sbin/crond -b 2>/dev/null && log "crond started" || warn "could not start crond"
}

print_cron_setup_hint() {
    cat <<'EOF'

Auto-sync scheduling is NOT active because MiSTer's cron daemon isn't running.
To enable it now AND at every boot, re-run the installer with --enable-cron:

    curl -kL https://raw.githubusercontent.com/borger/trove/main/install.sh | bash -s -- --enable-cron

Or do it by hand:

    # 1) Start crond right now (survives until reboot):
    /usr/sbin/crond -b

    # 2) Persist across reboots:
    echo '/usr/sbin/crond -b' >> /media/fat/linux/user-startup.sh
    chmod +x /media/fat/linux/user-startup.sh

Until crond is running, `trove sync` still works fine — just run it manually.

EOF
}

if [ "$NO_CRON" -eq 0 ]; then
    install_cron_entry
    if [ "${ENABLE_CROND:-0}" -eq 1 ]; then
        enable_crond_at_boot
        start_crond_now
    elif crond_is_running; then
        log "crond is running — auto-sync active"
    else
        warn "crond is not running — auto-sync WILL NOT fire until you start it"
        print_cron_setup_hint
    fi
else
    log "cron skipped (--no-cron)"
fi

# ── first-run guidance ────────────────────────────────────────────────────
cat <<EOF

$( log "installed OK." )

Next steps:
  1) Pair with your RomM server:
       $TROVE_HOME/bin/trove pair
  2) Edit config to subscribe to one or more collections:
       $TROVE_HOME/bin/trove config
  3) Try a dry-run sync first:
       $TROVE_HOME/bin/trove sync --dry-run
  4) When happy, run for real:
       $TROVE_HOME/bin/trove sync

Or launch the interactive menu from MiSTer's Scripts menu → 'trove.sh'.

EOF
