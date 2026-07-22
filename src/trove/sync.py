"""Trove sync engine — headless / policy-driven.

Same two-phase design as the mister-companion prototype, but with all UI
prompts replaced by configurable policies (``on_rom_conflict``,
``on_asset_conflict``, ``on_orphan``) since a headless cron job can't prompt
the user. Preserves the safety mechanics that made the prototype defensible:
manifest-scoped orphan detection, parent-managed upload gating, emulator
compatibility filter, newest-wins for saves/states, post-transfer mtime sync,
and intentional-deletion detection.

Manifest lives at ``<state_dir>/manifest.json`` and is rewritten atomically
after every sync. Entries are ``{path: {mtime, size, hash?}}``; legacy ``true``
values from earlier runs are treated as "we own this but lack details".
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .mister_cores import core_folder_for_slug, saves_dir, states_dir

# Timestamps within this window are treated as "equal" (clock skew / rounding).
_MTIME_TOLERANCE_SECONDS = 5

# Files smaller than this get no in-flight progress (they finish fast enough
# that a single ↓ line is enough). Anything bigger — or unknown-size — reports.
_PROGRESS_MIN_BYTES = 5 * 1024 * 1024   # 5 MB
_PROGRESS_MIN_INTERVAL = 1.5             # seconds between updates
_PROGRESS_MIN_PERCENT_STEP = 15          # or every N% delta, whichever hits first


# ── policy constants ───────────────────────────────────────────────────────
class RomConflictPolicy:
    ROMM_WINS = "romm_wins"
    MISTER_WINS = "mister_wins"
    SKIP = "skip"


class AssetConflictPolicy:
    NEWEST_WINS = "newest_wins"
    ROMM_WINS = "romm_wins"
    MISTER_WINS = "mister_wins"
    SKIP = "skip"


class OrphanPolicy:
    KEEP = "keep"
    DELETE = "delete"


class EmulatorFilter:
    MISTER_ONLY = "mister_only"
    ALLOW_ALL = "allow_all"


# ── data classes ───────────────────────────────────────────────────────────
@dataclass
class SyncAction:
    kind: str                      # download/overwrite/skip/orphan + save_/state_ variants
    core_folder: str
    remote_path: str               # on-MiSTer local path
    file_name: str
    size_romm: int = 0
    size_mister: int = 0
    rom_id: int | None = None
    rom_name: str = ""
    asset_id: int | None = None
    asset_emulator: str = ""
    asset_slot: str = ""
    asset_updated_at: str = ""


@dataclass
class UnsupportedSubscription:
    kind: str                      # "collection" only in v0.1 (platforms not yet subscribable)
    id: int
    name: str
    slug: str
    rom_count: int


@dataclass
class SyncPlan:
    actions: list[SyncAction] = field(default_factory=list)
    unsupported: list[UnsupportedSubscription] = field(default_factory=list)
    filtered_incompatible: int = 0

    def by_kind(self, kind: str) -> list[SyncAction]:
        return [a for a in self.actions if a.kind == kind]

    def totals(self) -> dict[str, int]:
        counts = {
            "download": 0, "overwrite": 0, "skip": 0, "orphan": 0,
            "save_download": 0, "save_upload": 0, "save_conflict": 0, "save_skip": 0,
            "state_download": 0, "state_upload": 0, "state_conflict": 0, "state_skip": 0,
        }
        for a in self.actions:
            if a.kind in counts:
                counts[a.kind] += 1
        counts["unsupported"] = len(self.unsupported)
        counts["bytes_to_download"] = sum(
            a.size_romm for a in self.actions if a.kind in ("download", "overwrite")
        )
        return counts


# ── local helpers ──────────────────────────────────────────────────────────
def _basename(fs_name: str) -> str:
    """ROM's basename without extension — matches MiSTer save file naming."""
    dot = fs_name.rfind(".")
    return fs_name[:dot] if dot > 0 else fs_name


def _parse_iso_to_unix(iso: str) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.timestamp()
    except Exception:
        return None


def _human_bytes(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


class _ProgressReporter:
    """Throttled per-file progress logger for large downloads.

    Emits INFO log lines like ``  ↳ 12.3MB/29.0MB (42%, 5.2MB/s, eta 3s)`` at
    most every ``_PROGRESS_MIN_INTERVAL`` seconds OR every
    ``_PROGRESS_MIN_PERCENT_STEP`` % of total, whichever fires first. Silent
    for files smaller than ``_PROGRESS_MIN_BYTES`` — no clutter for NES ROMs.
    """

    def __init__(self, log: Callable[[str], None] | None, total_bytes: int):
        self.log = log
        self.total = int(total_bytes or 0)
        # Enable progress reporting when file is big, OR when we don't know the
        # size at all (unknown size = we can't tell if it's small, safer to show).
        self.enabled = bool(log) and (self.total >= _PROGRESS_MIN_BYTES or self.total == 0)
        self.start = time.monotonic()
        self.last_time = self.start
        self.last_pct = -1

    def update(self, written: int) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        elapsed_since_last = now - self.last_time
        pct = int(written * 100 / self.total) if self.total else -1
        pct_step = pct - self.last_pct if self.total else 0
        if (
            elapsed_since_last < _PROGRESS_MIN_INTERVAL
            and pct_step < _PROGRESS_MIN_PERCENT_STEP
            and written < (self.total or float("inf"))
        ):
            return
        total_elapsed = now - self.start
        speed = written / total_elapsed if total_elapsed > 0 else 0
        if self.total:
            eta = int((self.total - written) / speed) if speed > 0 else 0
            self.log(
                f"  ↳ {_human_bytes(written)}/{_human_bytes(self.total)} "
                f"({pct}%, {_human_bytes(speed)}/s, eta {eta}s)"
            )
        else:
            self.log(f"  ↳ {_human_bytes(written)} ({_human_bytes(speed)}/s)")
        self.last_time = now
        self.last_pct = pct


def _local_md5(path: str) -> str | None:
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _romm_asset_md5(client, asset_kind: str, asset_id: int) -> str | None:
    """Stream a RomM save/state and compute its MD5 client-side."""
    try:
        resp = client.stream_asset(asset_kind, asset_id)
        h = hashlib.md5()
        try:
            for chunk in resp.iter_content():
                if chunk:
                    h.update(chunk)
        finally:
            resp.close()
        return h.hexdigest()
    except Exception:
        return None


def _manifest_entry_matches(entry, current_mtime, current_size) -> bool:
    if not isinstance(entry, dict):
        return False
    m_prev = entry.get("mtime")
    s_prev = entry.get("size")
    if m_prev is None or s_prev is None or current_mtime is None or current_size is None:
        return False
    return (
        abs(float(m_prev) - float(current_mtime)) <= _MTIME_TOLERANCE_SECONDS
        and int(s_prev) == int(current_size)
    )


def _asset_is_mister_compatible(asset: dict, emu_filter: str) -> bool:
    if emu_filter == EmulatorFilter.ALLOW_ALL:
        return True
    emu = (asset.get("emulator") or "").strip().lower()
    return emu == "" or emu == "mister"


def _relpath_key(remote_path: str, mister_root: str) -> str:
    if remote_path.startswith(mister_root + "/"):
        return remote_path[len(mister_root) + 1:]
    return remote_path


def _mkdir_p(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _local_listdir(dir_path: str) -> dict[str, int]:
    """Return {filename: size} for regular files in dir_path (empty if missing)."""
    result: dict[str, int] = {}
    try:
        for name in os.listdir(dir_path):
            full = os.path.join(dir_path, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not (st.st_mode & 0o170000 == 0o100000):  # not a regular file
                continue
            result[name] = int(st.st_size)
    except OSError:
        pass
    return result


# ── ROM planning ───────────────────────────────────────────────────────────
def _rom_file_name(rom: dict) -> str:
    return rom.get("fs_name") or rom.get("file_name") or ""


def _rom_is_folder(rom: dict) -> bool:
    if rom.get("multi_file"):
        return True
    files = rom.get("files") or []
    return len(files) > 1


# ── save/state planning ────────────────────────────────────────────────────
def _plan_asset_actions(
    client,
    rom: dict,
    core: str,
    asset_kind: str,
    remote_dir: str,
    plan: SyncPlan,
    remote_entries: dict[str, int],  # basename-matched MiSTer files
    *,
    parent_rom_managed: bool,
    readonly: bool,
    manifest: dict,
    mister_root: str,
    emulator_filter: str,
    upload_claimed: dict[str, tuple[int, str]],
    log: Callable[[str], None],
) -> None:
    rom_base = _basename(rom.get("fs_name") or "")
    if not rom_base:
        return
    getter = client.get_saves if asset_kind == "save" else client.get_states
    try:
        remote_assets = getter(rom["id"])
    except Exception as exc:
        log(f"  ! fetching {asset_kind}s for rom #{rom['id']}: {exc}")
        return

    matched_names: set[str] = set()

    for asset in remote_assets or []:
        fname = asset.get("file_name") or ""
        if not fname:
            continue
        if not _asset_is_mister_compatible(asset, emulator_filter):
            plan.filtered_incompatible += 1
            matched_names.add(fname)
            continue
        matched_names.add(fname)
        remote_path = f"{remote_dir}/{fname}"
        r_size = int(asset.get("file_size_bytes") or 0)
        m_size = remote_entries.get(fname)

        common = dict(
            core_folder=core, remote_path=remote_path, file_name=fname,
            rom_id=rom["id"],
            rom_name=rom.get("name") or rom.get("fs_name") or "",
            asset_id=asset.get("id"),
            asset_emulator=str(asset.get("emulator") or ""),
            asset_slot=str(asset.get("slot") or ""),
            asset_updated_at=str(asset.get("updated_at") or ""),
        )

        if m_size is None:
            plan.actions.append(SyncAction(kind=f"{asset_kind}_download",
                                           size_romm=r_size, **common))
            continue

        # Both sides have a file — decide identity → direction.
        r_hash = str(asset.get("content_hash") or "").lower()
        m_hash = _local_md5(remote_path)
        if r_hash and m_hash and r_hash == m_hash:
            plan.actions.append(SyncAction(kind=f"{asset_kind}_skip",
                                           size_romm=r_size, size_mister=m_size, **common))
            continue

        try:
            m_ts = float(os.stat(remote_path).st_mtime)
        except OSError:
            m_ts = None
        r_ts = _parse_iso_to_unix(asset.get("updated_at") or "")
        sizes_match = bool(r_size and m_size == r_size)
        both_ts = r_ts is not None and m_ts is not None

        # Manifest says MiSTer is unchanged AND RomM size still matches → skip.
        if sizes_match:
            entry = manifest.get(_relpath_key(remote_path, mister_root))
            if _manifest_entry_matches(entry, m_ts, m_size):
                plan.actions.append(SyncAction(kind=f"{asset_kind}_skip",
                                               size_romm=r_size, size_mister=m_size, **common))
                continue

        # Sizes match + mtimes match → identity (used for hashless states).
        if sizes_match and both_ts and abs(m_ts - r_ts) <= _MTIME_TOLERANCE_SECONDS:
            plan.actions.append(SyncAction(kind=f"{asset_kind}_skip",
                                           size_romm=r_size, size_mister=m_size, **common))
            continue

        # Streaming byte-hash tie-breaker for hashless states.
        if sizes_match and not r_hash and m_hash:
            streamed = _romm_asset_md5(client, asset_kind, asset.get("id"))
            if streamed and streamed == m_hash:
                plan.actions.append(SyncAction(kind=f"{asset_kind}_skip",
                                               size_romm=r_size, size_mister=m_size, **common))
                continue

        # Direction by timestamp.
        if both_ts:
            delta = m_ts - r_ts
            if delta > _MTIME_TOLERANCE_SECONDS:
                if readonly:
                    log(f"  read-only: MiSTer newer {asset_kind} '{fname}' not uploaded")
                    plan.actions.append(SyncAction(kind=f"{asset_kind}_skip",
                                                   size_romm=r_size, size_mister=m_size, **common))
                else:
                    plan.actions.append(SyncAction(kind=f"{asset_kind}_upload",
                                                   size_romm=r_size, size_mister=m_size, **common))
                continue
            if delta < -_MTIME_TOLERANCE_SECONDS:
                plan.actions.append(SyncAction(kind=f"{asset_kind}_download",
                                               size_romm=r_size, size_mister=m_size, **common))
                continue

        # Last-ditch: no hashes + no timestamps + sizes match → skip.
        if not both_ts and sizes_match and not r_hash and not m_hash:
            plan.actions.append(SyncAction(kind=f"{asset_kind}_skip",
                                           size_romm=r_size, size_mister=m_size, **common))
            continue

        plan.actions.append(SyncAction(kind=f"{asset_kind}_conflict",
                                       size_romm=r_size, size_mister=m_size, **common))

    # MiSTer-side files with no RomM counterpart → upload (unless readonly /
    # unmanaged / intentional-deletion pattern detected).
    if not parent_rom_managed or readonly:
        return
    for fname, sz in remote_entries.items():
        if fname in matched_names or not fname.startswith(rom_base):
            continue
        rest = fname[len(rom_base):]
        if rest and not (rest.startswith(".") or rest.startswith("_")):
            continue
        remote_path = f"{remote_dir}/{fname}"
        # Intentional-deletion detection
        entry = manifest.get(_relpath_key(remote_path, mister_root))
        if isinstance(entry, dict):
            try:
                cur_mt = float(os.stat(remote_path).st_mtime)
            except OSError:
                cur_mt = None
            if _manifest_entry_matches(entry, cur_mt, sz):
                log(f"  skip re-upload {asset_kind} '{fname}' — MiSTer unchanged, RomM copy deleted intentionally")
                continue
        # Duplicate-basename guard
        prior = upload_claimed.get(remote_path)
        if prior is not None:
            log(f"  ! ambiguous {asset_kind} '{fname}' — attributed to rom #{prior[0]} ({prior[1]}), skipping other")
            continue
        upload_claimed[remote_path] = (rom["id"], rom.get("name") or rom.get("fs_name") or "")

        slot = ""
        if asset_kind == "save" and rest.startswith("_"):
            middle = rest.lstrip("_").split(".", 1)[0]
            if middle.isdigit():
                slot = middle
        plan.actions.append(SyncAction(
            kind=f"{asset_kind}_upload",
            core_folder=core, remote_path=remote_path,
            file_name=fname, size_mister=int(sz),
            rom_id=rom["id"],
            rom_name=rom.get("name") or rom.get("fs_name") or "",
            asset_emulator="mister", asset_slot=slot,
        ))


# ── planning entry point ───────────────────────────────────────────────────
def plan_sync(
    client,
    *,
    subscribed_collection_ids: list[int],
    readonly_collection_ids: list[int] | None = None,
    manifest: dict | None = None,
    mister_root: str = "/media/fat",
    include_assets: bool = False,
    on_rom_conflict: str = RomConflictPolicy.ROMM_WINS,
    on_orphan: str = OrphanPolicy.KEEP,
    emulator_filter: str = EmulatorFilter.MISTER_ONLY,
    progress: Callable[[str], None] | None = None,
) -> SyncPlan:
    """Compute a SyncPlan against a live RomM + the local (on-MiSTer) filesystem.

    v0.1: collections-only subscriptions (no whole-platform sync).
    """
    log = progress or (lambda _s: None)
    plan = SyncPlan()
    manifest = manifest or {}
    readonly_collection_ids = set(readonly_collection_ids or [])
    games_root = f"{mister_root}/games"
    wanted_per_core: dict[str, list[dict]] = {}
    readonly_cores: set[str] = set()

    for cid in subscribed_collection_ids:
        log(f"listing RomM collection #{cid}")
        try:
            roms = client.get_roms(collection_id=cid)
        except Exception as exc:
            log(f"! collection #{cid}: {exc}")
            continue
        for rom in roms:
            slug = (rom.get("platform_fs_slug") or rom.get("platform_slug") or "").lower()
            core = core_folder_for_slug(slug)
            if core is None:
                if not any(u.slug == slug and u.kind == "collection" and u.id == cid for u in plan.unsupported):
                    plan.unsupported.append(
                        UnsupportedSubscription("collection", cid, rom.get("platform_name") or slug, slug, 0)
                    )
                continue
            wanted_per_core.setdefault(core, []).append(rom)
            if cid in readonly_collection_ids:
                readonly_cores.add(core)

    # Local (on-MiSTer) listing per core.
    remote_per_core: dict[str, dict[str, int]] = {}
    for core in wanted_per_core:
        remote_per_core[core] = _local_listdir(f"{games_root}/{core}")

    # ROM planning
    for core, roms in wanted_per_core.items():
        remote_files = remote_per_core[core]
        seen_files: set[str] = set()
        wanted_names: set[str] = set()
        for rom in roms:
            if _rom_is_folder(rom):
                log(f"skip (multi-file): {rom.get('name') or rom.get('fs_name')}")
                continue
            fname = _rom_file_name(rom)
            if not fname or fname in seen_files:
                continue
            seen_files.add(fname)
            wanted_names.add(fname)

            size_romm = int(rom.get("fs_size_bytes") or 0)
            sha1 = ""
            files = rom.get("files") or []
            if files:
                sha1 = str(files[0].get("sha1_hash") or "")
            sha1 = sha1 or str(rom.get("sha1_hash") or "")

            remote_size = remote_files.get(fname)
            remote_path = f"{games_root}/{core}/{fname}"

            if remote_size is None:
                plan.actions.append(SyncAction(
                    kind="download",
                    core_folder=core, remote_path=remote_path,
                    file_name=fname, size_romm=size_romm,
                    rom_id=rom.get("id"), rom_name=rom.get("name") or fname,
                ))
            elif size_romm and remote_size == size_romm:
                plan.actions.append(SyncAction(
                    kind="skip",
                    core_folder=core, remote_path=remote_path,
                    file_name=fname, size_romm=size_romm, size_mister=remote_size,
                    rom_id=rom.get("id"), rom_name=rom.get("name") or fname,
                ))
            else:
                plan.actions.append(SyncAction(
                    kind="overwrite",
                    core_folder=core, remote_path=remote_path,
                    file_name=fname, size_romm=size_romm, size_mister=remote_size,
                    rom_id=rom.get("id"), rom_name=rom.get("name") or fname,
                ))

        # Manifest-scoped orphan detection
        for fname, rsize in remote_files.items():
            if fname in wanted_names:
                continue
            remote_path = f"{games_root}/{core}/{fname}"
            if _relpath_key(remote_path, mister_root) not in manifest:
                continue  # pre-existing user content — leave alone
            plan.actions.append(SyncAction(
                kind="orphan",
                core_folder=core, remote_path=remote_path,
                file_name=fname, size_mister=int(rsize),
            ))

    # Save/state planning (opt-in)
    if include_assets:
        managed_rom_paths: set[str] = set(manifest.keys())
        for a in plan.actions:
            if a.kind in ("download", "skip", "overwrite"):
                managed_rom_paths.add(_relpath_key(a.remote_path, mister_root))

        save_claims: dict[str, tuple[int, str]] = {}
        state_claims: dict[str, tuple[int, str]] = {}

        for core, roms in wanted_per_core.items():
            slug_for_core = next(
                ((rom.get("platform_fs_slug") or rom.get("platform_slug") or "")
                 for rom in roms
                 if core_folder_for_slug((rom.get("platform_fs_slug") or rom.get("platform_slug") or "")) == core),
                "",
            )
            sdir = saves_dir(slug_for_core, root=mister_root)
            tdir = states_dir(slug_for_core, root=mister_root)
            if not sdir or not tdir:
                continue
            s_entries = _local_listdir(sdir)
            t_entries = _local_listdir(tdir)

            core_readonly = core in readonly_cores
            for rom in roms:
                if _rom_is_folder(rom):
                    continue
                rom_base = _basename(rom.get("fs_name") or "")
                if not rom_base:
                    continue
                s_for_rom = {n: sz for n, sz in s_entries.items()
                             if n == rom_base or n.startswith(rom_base + ".") or n.startswith(rom_base + "_")}
                t_for_rom = {n: sz for n, sz in t_entries.items()
                             if n == rom_base or n.startswith(rom_base + ".") or n.startswith(rom_base + "_")}
                rom_key = _relpath_key(f"{games_root}/{core}/{rom.get('fs_name') or ''}", mister_root)
                parent_managed = rom_key in managed_rom_paths
                _plan_asset_actions(
                    client, rom, core, "save",  sdir, plan, s_for_rom,
                    parent_rom_managed=parent_managed, readonly=core_readonly,
                    manifest=manifest, mister_root=mister_root,
                    emulator_filter=emulator_filter,
                    upload_claimed=save_claims, log=log,
                )
                _plan_asset_actions(
                    client, rom, core, "state", tdir, plan, t_for_rom,
                    parent_rom_managed=parent_managed, readonly=core_readonly,
                    manifest=manifest, mister_root=mister_root,
                    emulator_filter=emulator_filter,
                    upload_claimed=state_claims, log=log,
                )

    return plan


# ── execution ──────────────────────────────────────────────────────────────
def _download_stream_to(
    dest_path: str,
    iter_chunks,
    on_progress: Callable[[int], None] | None = None,
    compute_hash: bool = False,
) -> tuple[int, str | None]:
    """Write an iterator of bytes to a local path atomically (via .part rename).

    Returns (bytes_written, md5_hex or None). When ``compute_hash=True`` we
    accumulate MD5 during the write — this makes hashing essentially free
    (no separate file re-read), which matters a lot on MiSTer's SD card where
    a post-hoc ``md5sum`` on a multi-GB CHD would hang for minutes without
    any output. Callers use the returned hash to populate the manifest.
    """
    tmp = dest_path + ".part"
    _mkdir_p(os.path.dirname(dest_path))
    written = 0
    hasher = hashlib.md5() if compute_hash else None
    with open(tmp, "wb") as f:
        for chunk in iter_chunks:
            if not chunk:
                continue
            f.write(chunk)
            if hasher is not None:
                hasher.update(chunk)
            written += len(chunk)
            if on_progress:
                on_progress(written)
    os.replace(tmp, dest_path)
    return written, (hasher.hexdigest() if hasher else None)


def _sync_mtime_to_updated_at(local_path: str, updated_at_iso: str) -> None:
    """After a transfer, force local mtime = RomM updated_at.

    Lets the planner recognise "same content" via size+mtime identity — critical
    for states (RomM's states schema has no content_hash column).
    """
    ts = _parse_iso_to_unix(updated_at_iso)
    if ts is None:
        return
    try:
        os.utime(local_path, (ts, ts))
    except OSError:
        pass


def _download_rom(client, action: SyncAction, log: Callable[[str], None] | None = None) -> str | None:
    """Download a ROM. Returns the MD5 hex we computed inline (or None on failure)."""
    resp = client.stream_rom(action.rom_id, action.file_name)
    try:
        total = int(resp.headers.get("content-length", 0)) or int(action.size_romm or 0)
        reporter = _ProgressReporter(log, total)
        _, md5_hex = _download_stream_to(
            action.remote_path, resp.iter_content(),
            on_progress=reporter.update, compute_hash=True,
        )
    finally:
        resp.close()
    return md5_hex


def _download_asset(client, action: SyncAction, asset_kind: str, log: Callable[[str], None] | None = None) -> str | None:
    resp = client.stream_asset(asset_kind, action.asset_id)
    try:
        total = int(resp.headers.get("content-length", 0)) or int(action.size_romm or 0)
        reporter = _ProgressReporter(log, total)
        _, md5_hex = _download_stream_to(
            action.remote_path, resp.iter_content(),
            on_progress=reporter.update, compute_hash=True,
        )
    finally:
        resp.close()
    _sync_mtime_to_updated_at(action.remote_path, action.asset_updated_at)
    return md5_hex


def _upload_asset(client, action: SyncAction, asset_kind: str) -> None:
    with open(action.remote_path, "rb") as f:
        payload = f.read()
    if action.asset_id is not None:
        resp = client.update_asset(asset_kind, action.asset_id, action.file_name, payload)
    else:
        resp = client.upload_asset(
            asset_kind, action.rom_id, action.file_name, payload,
            emulator=action.asset_emulator or "mister",
            slot=action.asset_slot or None,
        )
    _sync_mtime_to_updated_at(action.remote_path, (resp or {}).get("updated_at", ""))


def execute_sync(
    plan: SyncPlan,
    client,
    *,
    manifest: dict,
    mister_root: str = "/media/fat",
    on_rom_conflict: str = RomConflictPolicy.ROMM_WINS,
    on_asset_conflict: str = AssetConflictPolicy.NEWEST_WINS,
    on_orphan: str = OrphanPolicy.KEEP,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Perform the plan. Returns counters dict; mutates manifest in place."""
    log = progress or (lambda _s: None)
    stats = {
        "downloaded": 0, "overwritten": 0, "deleted": 0, "skipped": 0, "kept": 0,
        "saves_down": 0, "saves_up": 0, "saves_skip": 0,
        "states_down": 0, "states_up": 0, "states_skip": 0,
        "errors": 0,
    }

    def _snapshot(remote_path: str, precomputed_hash: str | None = None) -> dict:
        # mtime+size only — hash is populated only when the caller has one from
        # inline computation during download. Re-reading a multi-GB file just to
        # md5 it was the source of the multi-minute post-download hang.
        entry: dict = {}
        try:
            st = os.stat(remote_path)
            entry["mtime"] = float(st.st_mtime)
            entry["size"] = int(st.st_size)
        except OSError:
            pass
        if precomputed_hash:
            entry["hash"] = precomputed_hash
        return entry

    def _mark_placed(a: SyncAction, precomputed_hash: str | None = None) -> None:
        manifest[_relpath_key(a.remote_path, mister_root)] = _snapshot(a.remote_path, precomputed_hash)

    def _mark_removed(a: SyncAction) -> None:
        manifest.pop(_relpath_key(a.remote_path, mister_root), None)

    for a in plan.actions:
        try:
            if a.kind == "skip":
                _mark_placed(a); stats["skipped"] += 1

            elif a.kind == "download":
                log(f"↓ {a.rom_name}  ({_human_bytes(a.size_romm)})")
                h = _download_rom(client, a, log=log); _mark_placed(a, h); stats["downloaded"] += 1

            elif a.kind == "overwrite":
                if on_rom_conflict == RomConflictPolicy.ROMM_WINS:
                    log(f"↓ overwrite {a.rom_name} ({_human_bytes(a.size_romm)}, romm wins)")
                    h = _download_rom(client, a, log=log); _mark_placed(a, h); stats["overwritten"] += 1
                elif on_rom_conflict == RomConflictPolicy.MISTER_WINS:
                    log(f"= keep MiSTer copy of {a.file_name}"); stats["skipped"] += 1
                else:
                    log(f"? unresolved overwrite {a.file_name} — skip policy"); stats["skipped"] += 1

            elif a.kind == "orphan":
                if on_orphan == OrphanPolicy.DELETE:
                    log(f"× delete orphan {a.remote_path}")
                    try:
                        os.remove(a.remote_path); _mark_removed(a); stats["deleted"] += 1
                    except OSError as exc:
                        log(f"  remove failed: {exc}"); stats["errors"] += 1
                else:
                    stats["kept"] += 1

            elif a.kind in ("save_skip", "state_skip"):
                _mark_placed(a)
                stats["saves_skip" if a.kind == "save_skip" else "states_skip"] += 1

            elif a.kind == "save_download":
                log(f"↓ save {a.file_name} ({_human_bytes(a.size_romm)})")
                h = _download_asset(client, a, "save", log=log)
                _mark_placed(a, h); stats["saves_down"] += 1
            elif a.kind == "state_download":
                log(f"↓ state {a.file_name} ({_human_bytes(a.size_romm)})")
                h = _download_asset(client, a, "state", log=log)
                _mark_placed(a, h); stats["states_down"] += 1

            elif a.kind == "save_upload":
                log(f"↑ save {a.file_name}"); _upload_asset(client, a, "save")
                stats["saves_up"] += 1
            elif a.kind == "state_upload":
                log(f"↑ state {a.file_name}"); _upload_asset(client, a, "state")
                stats["states_up"] += 1

            elif a.kind in ("save_conflict", "state_conflict"):
                kind = "save" if a.kind == "save_conflict" else "state"
                # Policy-driven conflict resolution (no prompts in headless).
                if on_asset_conflict == AssetConflictPolicy.NEWEST_WINS:
                    # planner would've picked direction; conflict means times equal → prefer RomM.
                    log(f"↓ {kind} conflict → romm wins (times ambiguous) {a.file_name}")
                    h = _download_asset(client, a, kind, log=log); _mark_placed(a, h)
                    stats["saves_down" if kind == "save" else "states_down"] += 1
                elif on_asset_conflict == AssetConflictPolicy.ROMM_WINS:
                    log(f"↓ {kind} conflict → romm wins (policy) {a.file_name}")
                    h = _download_asset(client, a, kind, log=log); _mark_placed(a, h)
                    stats["saves_down" if kind == "save" else "states_down"] += 1
                elif on_asset_conflict == AssetConflictPolicy.MISTER_WINS:
                    log(f"↑ {kind} conflict → mister wins (policy) {a.file_name}")
                    _upload_asset(client, a, kind)
                    stats["saves_up" if kind == "save" else "states_up"] += 1
                else:
                    log(f"? {kind} conflict {a.file_name} — skip policy"); stats["errors"] += 1

        except Exception as exc:
            log(f"! {a.file_name}: {exc}"); stats["errors"] += 1

    return stats


# ── firmware / BIOS download ───────────────────────────────────────────────
def download_firmware_for_platform(
    client,
    *,
    platform_id: int,
    platform_slug: str,
    mister_root: str = "/media/fat",
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Pull all firmware for a platform into <mister_root>/games/<Core>/bios/."""
    log = progress or (lambda _s: None)
    stats = {"downloaded": 0, "skipped": 0, "errors": 0, "bytes": 0}
    core = core_folder_for_slug(platform_slug)
    if core is None:
        log(f"! platform '{platform_slug}' has no MiSTer core — skipping BIOS")
        return stats
    remote_dir = f"{mister_root}/games/{core}/bios"

    try:
        firmware = client.get_firmware(platform_id) or []
    except Exception as exc:
        log(f"! fetch firmware list failed: {exc}"); stats["errors"] += 1
        return stats
    if not firmware:
        log(f"no firmware in RomM for '{platform_slug}' (core: {core})")
        return stats

    log(f"↓ {len(firmware)} BIOS file(s) → {remote_dir}/")
    _mkdir_p(remote_dir)

    for fw in firmware:
        fname = fw.get("file_name") or ""
        fid = fw.get("id")
        r_size = int(fw.get("file_size_bytes") or 0)
        if not fname or fid is None:
            continue
        remote_path = f"{remote_dir}/{fname}"
        try:
            m_size = int(os.stat(remote_path).st_size)
            if r_size and m_size == r_size:
                log(f"  = {fname} (unchanged, {r_size} B)")
                stats["skipped"] += 1
                continue
        except OSError:
            pass
        try:
            log(f"  ↓ {fname} ({_human_bytes(r_size)})")
            resp = client.stream_firmware(fid, fname)
            try:
                total = int(resp.headers.get("content-length", 0)) or r_size
                reporter = _ProgressReporter(log, total)
                _download_stream_to(remote_path, resp.iter_content(),
                                    on_progress=reporter.update, compute_hash=False)
            finally:
                resp.close()
            stats["downloaded"] += 1
            stats["bytes"] += r_size
        except Exception as exc:
            log(f"  ! {fname}: {exc}")
            stats["errors"] += 1
    return stats
