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

log()  { printf '\033[36m[trove]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[trove]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[trove]\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dev)         DEV_SOURCE="${2:-}"; shift 2 ;;
        --source)      DEV_SOURCE="${2:-}"; shift 2 ;;
        --no-cron)     NO_CRON=1; shift ;;
        --home)        TROVE_HOME="${2:-}"; shift 2 ;;
        --tag)         TROVE_TAG="${2:-}"; shift 2 ;;
        -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)             die "unknown flag: $1" ;;
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
if [ "$NO_CRON" -eq 0 ]; then
    if command -v crontab >/dev/null 2>&1; then
        SCHEDULE="$(python3 -c "import json,sys; c=json.load(open('$TROVE_HOME/config.json')) if __import__('os').path.exists('$TROVE_HOME/config.json') else {}; print(((c.get('cron') or {}).get('schedule')) or '0 3 * * *')" 2>/dev/null || echo "0 3 * * *")"
        CMD="$TROVE_HOME/bin/trove sync --quiet"
        MARK="# TROVE"
        # Remove any prior trove line, then append fresh
        (crontab -l 2>/dev/null | grep -v "$MARK"; echo "$SCHEDULE  $CMD  $MARK") | crontab -
        log "cron entry set: $SCHEDULE  $CMD"
    else
        warn "crontab not available — skipping auto-sync cron entry"
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
