#!/usr/bin/env bash
# Cut a Trove release — bumps VERSION, commits, tags, pushes, publishes.
#
# Usage:
#   ./release.sh <version> "<title-suffix>"                   # opens $EDITOR for notes
#   ./release.sh <version> "<title-suffix>" -f notes.md       # notes from file
#   ./release.sh <version> "<title-suffix>" -m "inline notes" # notes inline
#
# Notes are ALWAYS required — a bare commit list is not a release description.
# The editor path pre-fills the buffer with a template + the raw commit list
# since the previous tag, so you can rewrite it into human prose.
set -euo pipefail

if [ $# -lt 2 ]; then
    cat >&2 <<EOF
usage: $0 <version> "<title-suffix>" [-f <notes-file> | -m "<inline notes>"]

    $0 0.1.7 "New browse UI"                      # opens \$EDITOR
    $0 0.1.7 "New browse UI" -f notes/v0.1.7.md   # notes from file
    $0 0.1.7 "New browse UI" -m "Short note."     # inline
EOF
    exit 2
fi

VERSION="$1"
TITLE_SUFFIX="$2"
NOTES_FILE=""
NOTES_INLINE=""
shift 2
while [ $# -gt 0 ]; do
    case "$1" in
        -f) NOTES_FILE="${2:-}"; shift 2 ;;
        -m) NOTES_INLINE="${2:-}"; shift 2 ;;
        *) echo "error: unknown flag: $1" >&2; exit 2 ;;
    esac
done

TAG="v${VERSION}"

# Basic guard: refuse if version doesn't look like semver-ish
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.].+)?$ ]]; then
    echo "error: version '$VERSION' doesn't look like semver (X.Y.Z)" >&2
    exit 2
fi

# Refuse if there are uncommitted changes to anything other than VERSION
DIRTY="$(git status --porcelain | grep -v '^.M VERSION$' || true)"
if [ -n "$DIRTY" ]; then
    echo "error: working tree is dirty (uncommitted changes beyond VERSION):" >&2
    echo "$DIRTY" >&2
    exit 2
fi

# ── prepare release notes ─────────────────────────────────────────────────
NOTES_TMP="$(mktemp -t trove-release-notes.XXXXXX.md)"
trap 'rm -f "$NOTES_TMP"' EXIT

if [ -n "$NOTES_FILE" ]; then
    if [ ! -r "$NOTES_FILE" ]; then
        echo "error: notes file '$NOTES_FILE' not readable" >&2; exit 2
    fi
    cp "$NOTES_FILE" "$NOTES_TMP"
elif [ -n "$NOTES_INLINE" ]; then
    printf '%s\n' "$NOTES_INLINE" > "$NOTES_TMP"
else
    # Interactive: seed the buffer with a bare template + the raw commit log
    # since the previous tag. Rewrite as prose in the editor before saving.
    PREV_TAG="$(git tag --list 'v*' --sort=-v:refname | head -1 || true)"
    {
        printf '## What'"'"'s new\n\n\n\n'
        printf '## Upgrade\n\n'
        printf '```\ncurl -kL https://raw.githubusercontent.com/borger/trove/%s/install.sh | bash\n```\n' "$TAG"
        printf '\n'
        if [ -n "$PREV_TAG" ]; then
            printf '---\n'
            printf 'commits since %s (delete this block before saving):\n\n' "$PREV_TAG"
            git log --pretty=format:'* %s' "${PREV_TAG}..HEAD" 2>/dev/null
            printf '\n'
        fi
    } > "$NOTES_TMP"
    EDITOR_CMD="${EDITOR:-vi}"
    echo "release: opening \$EDITOR ($EDITOR_CMD) on notes template — save & quit when done"
    "$EDITOR_CMD" "$NOTES_TMP"
fi

# Refuse empty / template-only notes
if ! grep -qE '^[^<#[:space:]]' "$NOTES_TMP"; then
    echo "error: release notes are empty (or only contain HTML comments/headings)" >&2
    echo "       aborting to avoid publishing a description-less release." >&2
    exit 3
fi

# ── bump VERSION + commit ─────────────────────────────────────────────────
echo "release: bumping VERSION → $VERSION"
echo "$VERSION" > VERSION
git add VERSION
git commit -m "release: v${VERSION}"

echo "release: tagging + pushing"
git tag -a "$TAG" -m "release $TAG"
git push origin main
git push origin "$TAG"

TITLE="$TAG"
[ -n "$TITLE_SUFFIX" ] && TITLE="$TAG — $TITLE_SUFFIX"
echo "release: publishing on GitHub"
gh release create "$TAG" \
    --title "$TITLE" \
    --notes-file "$NOTES_TMP"

echo "release: v${VERSION} published — https://github.com/borger/trove/releases/tag/$TAG"
