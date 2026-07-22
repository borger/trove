"""Trove CLI — dispatcher for pair / sync / bios / status / config / update / doctor."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from . import __version__, config
from .logging_setup import setup as setup_logging
from .mister_cores import apply_overrides, core_folder_for_slug, is_supported
from .romm import RomMClient, RomMError, RomMPairingError, normalize_pairing_code
from .sync import (
    download_firmware_for_platform,
    execute_sync,
    plan_sync,
)
from .updater import apply_update, check as check_update


# ── helpers ────────────────────────────────────────────────────────────────
def _client(cfg: dict) -> RomMClient:
    romm = cfg.get("romm") or {}
    return RomMClient(romm.get("url", ""), token=romm.get("token") or "")


def _lockfile() -> Path:
    return config.home_dir() / "trove.lock"


def _acquire_lock(log) -> object | None:
    """Best-effort exclusive lock via fcntl. Returns the file handle to hold,
    or None if already held (caller should exit)."""
    import fcntl
    lf = _lockfile()
    lf.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lf, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error("Another Trove operation is already running (lock: %s).", lf)
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _apply_overrides_from_cfg(cfg: dict) -> None:
    apply_overrides(cfg.get("core_overrides"))


# ── commands ───────────────────────────────────────────────────────────────
def cmd_pair(args, log) -> int:
    cfg = config.load()
    url = args.url or (cfg.get("romm") or {}).get("url") or ""
    if not url:
        url = input("RomM URL (e.g. https://romm.example.com): ").strip()
    code = args.code or input("Pairing code (from RomM → Control Panel → API Keys): ").strip()
    normalized = normalize_pairing_code(code)
    if not url or not normalized:
        log.error("URL and pairing code are required.")
        return 2

    client = RomMClient(url)
    try:
        payload = client.exchange_pairing_code(normalized)
        me = client.whoami()
    except RomMPairingError as e:
        log.error("Pairing failed: %s", e)
        return 3
    except RomMError as e:
        log.error("RomM error: %s", e)
        return 3

    who = me.get("username") or me.get("email") or f"user #{me.get('id', '?')}"
    cfg.setdefault("romm", {})
    cfg["romm"]["url"] = url
    cfg["romm"]["token"] = payload.get("raw_token", "")
    config.save(cfg)
    log.info("Paired as %s. Token stored. (Token name: %s)",
             who, payload.get("name", "(unnamed)"))
    log.info("Next: subscribe to a collection in `trove config`, then `trove sync`.")
    return 0


def cmd_sync(args, log) -> int:
    cfg = config.load()
    _apply_overrides_from_cfg(cfg)
    problems = config.validate(cfg)
    if problems:
        for p in problems:
            log.error("config: %s", p)
        return 2

    client = _client(cfg)
    manifest = config.load_manifest()
    subs = cfg["subscriptions"]["collections"]
    sync_cfg = cfg["sync"]

    lock = _acquire_lock(log)
    if lock is None:
        return 4

    try:
        log.info("Planning sync — %d collection(s), saves&states: %s, root: %s",
                 len(subs), sync_cfg.get("saves_and_states"), cfg["mister"]["root"])
        plan = plan_sync(
            client,
            subscribed_collection_ids=subs,
            manifest=manifest,
            mister_root=cfg["mister"]["root"],
            include_assets=bool(sync_cfg.get("saves_and_states")),
            on_rom_conflict=sync_cfg["on_rom_conflict"],
            on_orphan=sync_cfg["on_orphan"],
            emulator_filter=sync_cfg["emulator_filter"],
            progress=log.info,
        )

        t = plan.totals()
        log.info(
            "Plan — ROMs: %d down / %d conflict / %d unchanged / %d orphan. "
            "Saves: %d↓/%d↑/%d⚔. States: %d↓/%d↑/%d⚔. Unsupported: %d. Bytes: %d",
            t["download"], t["overwrite"], t["skip"], t["orphan"],
            t["save_download"], t["save_upload"], t["save_conflict"],
            t["state_download"], t["state_upload"], t["state_conflict"],
            t["unsupported"], t["bytes_to_download"],
        )
        if plan.filtered_incompatible:
            log.info("  filtered %d save(s)/state(s) tagged for other emulators",
                     plan.filtered_incompatible)
        if plan.unsupported:
            names = ", ".join(f"{u.name} ({u.slug})" for u in plan.unsupported[:5])
            more = f" (+{len(plan.unsupported)-5} more)" if len(plan.unsupported) > 5 else ""
            log.info("  unsupported sources skipped: %s%s", names, more)

        if args.dry_run:
            log.info("(dry-run — no changes made)")
            return 0

        stats = execute_sync(
            plan, client,
            manifest=manifest,
            mister_root=cfg["mister"]["root"],
            on_rom_conflict=sync_cfg["on_rom_conflict"],
            on_asset_conflict=sync_cfg["on_asset_conflict"],
            on_orphan=sync_cfg["on_orphan"],
            progress=log.info,
        )
        config.save_manifest(manifest)
        summary = ", ".join(f"{k}={v}" for k, v in stats.items() if v)
        log.info("Sync complete: %s", summary or "nothing to do")
        return 0 if stats["errors"] == 0 else 1
    finally:
        lock.close()


def cmd_bios(args, log) -> int:
    cfg = config.load()
    _apply_overrides_from_cfg(cfg)
    if not (cfg.get("romm") or {}).get("token"):
        log.error("Not paired with RomM. Run `trove pair` first.")
        return 2
    client = _client(cfg)
    root = cfg["mister"]["root"]

    lock = _acquire_lock(log)
    if lock is None:
        return 4
    try:
        target = args.slug
        if target == "all":
            # walk all subscribed collections, extract unique platforms
            slugs: set[tuple[int, str]] = set()
            for cid in cfg["subscriptions"]["collections"]:
                try:
                    roms = client.get_roms(collection_id=cid)
                except RomMError as e:
                    log.error("collection #%d: %s", cid, e); continue
                for rom in roms:
                    slug = (rom.get("platform_fs_slug") or rom.get("platform_slug") or "").lower()
                    pid = rom.get("platform_id")
                    if slug and pid and is_supported(slug):
                        slugs.add((pid, slug))
            if not slugs:
                log.info("No subscribed platforms found."); return 0
            for pid, slug in sorted(slugs, key=lambda x: x[1]):
                download_firmware_for_platform(
                    client, platform_id=pid, platform_slug=slug,
                    mister_root=root, progress=log.info,
                )
            return 0
        else:
            # Single-slug: resolve platform_id via /api/platforms
            plats = {p.get("slug", "").lower(): p for p in client.get_platforms()}
            p = plats.get(target.lower())
            if not p:
                log.error("Unknown platform slug: %s", target); return 2
            download_firmware_for_platform(
                client, platform_id=p["id"], platform_slug=p.get("slug", ""),
                mister_root=root, progress=log.info,
            )
            return 0
    finally:
        lock.close()


def cmd_status(args, log) -> int:
    cfg = config.load()
    romm = cfg.get("romm") or {}
    log.info("Trove v%s", __version__)
    log.info("  home: %s", config.home_dir())
    log.info("  RomM URL: %s", romm.get("url") or "(not set)")
    log.info("  Paired: %s", "yes" if romm.get("token") else "no — run `trove pair`")
    log.info("  MiSTer root: %s", cfg["mister"]["root"])
    subs = cfg["subscriptions"]["collections"]
    log.info("  Subscribed collections: %s", ", ".join(map(str, subs)) if subs else "(none)")
    log.info("  Saves & states sync: %s", cfg["sync"]["saves_and_states"])
    manifest = config.load_manifest()
    log.info("  Manifest entries: %d", len(manifest))
    problems = config.validate(cfg)
    if problems:
        log.info("  Config problems:")
        for p in problems:
            log.info("    - %s", p)
    return 0


def cmd_config(args, log) -> int:
    editor = os.environ.get("EDITOR") or "vi"
    path = config.config_path()
    if not path.exists():
        cfg = config.load()  # creates default
    log.info("Editing %s (editor: %s) — Ctrl-C to abort.", path, editor)
    try:
        subprocess.call([editor, str(path)])
    except FileNotFoundError:
        log.error("Editor '%s' not found. Set $EDITOR or install vi.", editor)
        return 2
    # Reload + validate after editing
    cfg = config.load()
    problems = config.validate(cfg)
    if problems:
        log.info("Post-edit validation:")
        for p in problems:
            log.info("  - %s", p)
    else:
        log.info("Config OK.")
    return 0


def cmd_update(args, log) -> int:
    try:
        installed, latest, avail = check_update()
    except Exception as e:
        log.error("Update check failed: %s", e); return 3
    log.info("Installed: %s  Latest: %s", installed, latest or "(unknown)")
    if not avail:
        log.info("Already up to date.")
        return 0
    if args.check:
        log.info("Update available. Run `trove update` (without --check) to apply.")
        return 0
    install_root = Path(__file__).resolve().parent.parent.parent
    log.info("Applying update → %s", install_root)
    try:
        new_tag = apply_update(install_root)
    except Exception as e:
        log.error("Update failed: %s", e); return 3
    log.info("Updated to %s. Restart any long-running processes.", new_tag)
    return 0


def cmd_doctor(args, log) -> int:
    """Sanity-check the install."""
    ok = True
    cfg = config.load()
    log.info("Trove v%s doctor check", __version__)

    # Config
    problems = config.validate(cfg)
    if problems:
        ok = False
        log.info("  config: %d problem(s)", len(problems))
        for p in problems:
            log.info("    - %s", p)
    else:
        log.info("  config: OK")

    # RomM reachability
    romm_url = (cfg.get("romm") or {}).get("url")
    if romm_url:
        try:
            client = RomMClient(romm_url, token=(cfg.get("romm") or {}).get("token") or "")
            hb = client.heartbeat()
            log.info("  RomM reachable: %s (RomM %s)", romm_url, hb.get("SYSTEM", {}).get("VERSION", "?"))
        except Exception as e:
            ok = False
            log.info("  RomM unreachable: %s", e)

    # MiSTer path
    root = Path(cfg["mister"]["root"])
    games = root / "games"
    if not root.exists():
        ok = False
        log.info("  MiSTer root missing: %s", root)
    elif not games.exists():
        log.info("  MiSTer root exists but %s does not (empty game library?)", games)
    else:
        try:
            free = subprocess.check_output(["df", "-h", str(root)]).decode().splitlines()[-1].split()
            log.info("  MiSTer root OK: %s (free: %s)", root, free[3] if len(free) > 3 else "?")
        except Exception:
            log.info("  MiSTer root OK: %s", root)

    # Cron entry
    try:
        out = subprocess.check_output(["crontab", "-l"], stderr=subprocess.DEVNULL).decode()
        if "trove" in out.lower():
            log.info("  cron: entry found")
        else:
            log.info("  cron: no trove entry (auto-sync disabled)")
    except (subprocess.CalledProcessError, FileNotFoundError):
        log.info("  cron: not configured")

    # Update check
    try:
        _, latest, avail = check_update()
        if avail:
            log.info("  update: available (%s)", latest)
        else:
            log.info("  update: up to date")
    except Exception as e:
        log.info("  update: check failed (%s)", e)

    return 0 if ok else 1


# ── entry ──────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trove", description="Trove — RomM ↔ MiSTer sync.")
    p.add_argument("--version", action="version", version=f"trove {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging")
    p.add_argument("-q", "--quiet", action="store_true", help="silence console output (file log still writes)")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_pair = sub.add_parser("pair", help="pair with a RomM instance via short code")
    p_pair.add_argument("--url", help="RomM URL (prompted if omitted)")
    p_pair.add_argument("--code", help="pairing code (prompted if omitted)")
    p_pair.set_defaults(func=cmd_pair)

    p_sync = sub.add_parser("sync", help="run a sync against configured collections")
    p_sync.add_argument("--dry-run", action="store_true", help="plan only, no changes")
    p_sync.set_defaults(func=cmd_sync)

    p_bios = sub.add_parser("bios", help="download BIOS/firmware for a platform (or 'all' subscribed)")
    p_bios.add_argument("slug", help="platform slug (e.g. 'psx', 'saturn') or 'all'")
    p_bios.set_defaults(func=cmd_bios)

    p_status = sub.add_parser("status", help="show current install + subscription summary")
    p_status.set_defaults(func=cmd_status)

    p_config = sub.add_parser("config", help="edit config.json in $EDITOR")
    p_config.set_defaults(func=cmd_config)

    p_update = sub.add_parser("update", help="self-update from GitHub Releases")
    p_update.add_argument("--check", action="store_true", help="report only, don't apply")
    p_update.set_defaults(func=cmd_update)

    p_doctor = sub.add_parser("doctor", help="sanity-check the install")
    p_doctor.set_defaults(func=cmd_doctor)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = "DEBUG" if args.verbose else "INFO"
    log = setup_logging(level=level, quiet=args.quiet)
    try:
        return args.func(args, log)
    except KeyboardInterrupt:
        log.error("Interrupted."); return 130
    except Exception as e:
        log.exception("Unhandled error: %s", e); return 1


if __name__ == "__main__":
    sys.exit(main())
