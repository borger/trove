"""Self-update via GitHub Releases.

Compares the local VERSION file to the latest release tag on GitHub. On update,
downloads the release tarball, extracts to a temp directory, and swaps the
install atomically.
"""
from __future__ import annotations

import io
import json
import shutil
import ssl
import tarfile
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__

REPO = "borger/trove"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


def _get(url: str) -> bytes:
    ctx = ssl.create_default_context()
    req = Request(url, headers={"User-Agent": f"Trove/{__version__}"})
    with urlopen(req, timeout=15, context=ctx) as r:
        return r.read()


def latest_release() -> dict:
    """Return the GitHub release JSON for the latest tag."""
    try:
        return json.loads(_get(API_LATEST).decode())
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"GitHub check failed: {e}") from e


def check() -> tuple[str, str, bool]:
    """(installed, latest, update_available)."""
    installed = __version__
    latest = latest_release().get("tag_name", "").lstrip("v")
    return installed, latest, bool(latest and latest != installed)


def apply_update(install_root: Path) -> str:
    """Fetch the latest tarball, extract, and swap into ``install_root``.

    ``install_root`` is the directory that holds ``src/trove/``, ``VERSION``,
    etc. Preserves ``config.json``, ``manifest.json``, and ``logs/`` from the
    install by design (they live under ``$TROVE_HOME`` which is typically
    ``install_root`` on MiSTer but can be relocated).
    """
    rel = latest_release()
    tag = rel.get("tag_name", "").lstrip("v")
    tarball_url = rel.get("tarball_url")
    if not tag or not tarball_url:
        raise RuntimeError("No usable release payload from GitHub")

    raw = _get(tarball_url)
    with tempfile.TemporaryDirectory() as td:
        extract_root = Path(td) / "extract"
        extract_root.mkdir()
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            tf.extractall(extract_root)
        # GitHub tarballs are wrapped in one top-level dir like "borger-trove-abc123"
        top = next(extract_root.iterdir())

        preserve = {"config.json", "manifest.json", "logs"}
        for item in install_root.iterdir():
            if item.name in preserve:
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        for item in top.iterdir():
            dest = install_root / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
    return tag
