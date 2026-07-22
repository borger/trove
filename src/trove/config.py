"""Trove config — JSON on disk, validated on load.

The config file lives at ``$TROVE_HOME/config.json`` (default:
``/media/fat/Scripts/.trove/config.json``). The manifest lives alongside it
as ``manifest.json``. Both are atomically rewritten via .tmp + rename.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_HOME = "/media/fat/Scripts/.trove"

DEFAULT_CONFIG: dict = {
    "version": 1,
    "romm": {"url": "", "token": None},
    "mister": {"root": "/media/fat"},
    "subscriptions": {"collections": []},
    "sync": {
        "saves_and_states": True,
        "on_rom_conflict": "romm_wins",
        "on_asset_conflict": "newest_wins",
        "on_orphan": "keep",
        "emulator_filter": "mister_only",
    },
    "cron": {"schedule": "0 3 * * *", "enabled": True},
    "logging": {"level": "INFO", "max_bytes": 5_242_880, "backup_count": 3},
    "core_overrides": {},
}


def home_dir() -> Path:
    return Path(os.environ.get("TROVE_HOME", DEFAULT_HOME))


def config_path() -> Path:
    return home_dir() / "config.json"


def manifest_path() -> Path:
    return home_dir() / "manifest.json"


def logs_dir() -> Path:
    return home_dir() / "logs"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    """Load config from disk merged over defaults. Creates a stub if absent."""
    p = config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        save(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    return _deep_merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})


def save(cfg: dict) -> None:
    """Atomically write the config file."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=False))
    os.replace(tmp, p)


def load_manifest() -> dict:
    p = manifest_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(manifest: dict) -> None:
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp, p)


def validate(cfg: dict) -> list[str]:
    """Return a list of problems found (empty list = valid)."""
    problems: list[str] = []
    romm = cfg.get("romm") or {}
    if not romm.get("url"):
        problems.append("romm.url is empty — run `trove pair` to set it")
    mister = cfg.get("mister") or {}
    if not str(mister.get("root", "")).startswith("/"):
        problems.append("mister.root must be an absolute path (default /media/fat)")
    subs = cfg.get("subscriptions", {}).get("collections") or []
    if not subs:
        problems.append("subscriptions.collections is empty — no collections will sync")
    sync = cfg.get("sync") or {}
    valid_rom = {"romm_wins", "mister_wins", "skip"}
    if sync.get("on_rom_conflict") not in valid_rom:
        problems.append(f"sync.on_rom_conflict must be one of {sorted(valid_rom)}")
    valid_asset = {"newest_wins", "romm_wins", "mister_wins", "skip"}
    if sync.get("on_asset_conflict") not in valid_asset:
        problems.append(f"sync.on_asset_conflict must be one of {sorted(valid_asset)}")
    valid_orphan = {"keep", "delete"}
    if sync.get("on_orphan") not in valid_orphan:
        problems.append(f"sync.on_orphan must be one of {sorted(valid_orphan)}")
    valid_emu = {"mister_only", "allow_all"}
    if sync.get("emulator_filter") not in valid_emu:
        problems.append(f"sync.emulator_filter must be one of {sorted(valid_emu)}")
    return problems
