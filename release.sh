#!/usr/bin/env bash
# Cut a Trove release — bumps VERSION, commits, tags, pushes, publishes.
#
# Usage: ./release.sh 0.1.3 "One-line release blurb"
#
# The version bump commit ensures the tarball payload's VERSION file matches
# the release tag, so `trove --version` reflects reality after a self-update.
set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <version> [release-title-suffix]" >&2
    echo "  e.g.  $0 0.1.3 'Live download progress'" >&2
    exit 2
fi

VERSION="$1"
TITLE_SUFFIX="${2:-}"
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
    --generate-notes

echo "release: v${VERSION} published — https://github.com/borger/trove/releases/tag/$TAG"
