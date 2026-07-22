# Trove

A tiny, headless MiSTer companion that keeps your MiSTer library in sync with a self-hosted [RomM](https://github.com/rommapp/romm) server.

Ships as a single-file installer, needs no compilation, no pip, no Docker, and no external Python packages — just what's already on a stock MiSTer install.

## What it does today (v0.1)

- **Pair** with your RomM instance via a 60-second short code (no password stored).
- **Sync one or more collections** from RomM to `/media/fat/games/<Core>/`, matching MiSTer's folder conventions.
- **Two-way saves & states** — new MiSTer saves upload to RomM, RomM edits pull down. Optional; disabled by default until you've tried a dry-run.
- **BIOS/firmware download** per platform (or every subscribed platform in one shot).
- **Cron auto-sync** — installer schedules a nightly run; the schedule is configurable.
- **Self-update** — `trove update` grabs the latest release from GitHub.
- **Doctor** — `trove doctor` checks RomM reachability, MiSTer paths, cron entry, and disk space.

## What it explicitly won't do

- Delete anything it didn't place there. Files you had before installing Trove are invisible to its cleanup logic — they stay put unless you deliberately remove them.
- Auto-update itself. Updates only happen when you run `trove update`.

## Requirements

- MiSTer FPGA with the standard Linux userland (`python3` present — this comes with the default MiSTer distribution).
- A running [RomM](https://github.com/rommapp/romm) instance (v5.0.0 or later) reachable from the MiSTer's network.
- One RomM API token — Trove will help you create it on first run.

Trove uses only the Python standard library. There is nothing to `pip install`.

## Install

From your MiSTer (SSH in, or via the Scripts menu with an `install_trove.sh` script):

```bash
curl -kL https://raw.githubusercontent.com/borger/trove/main/install.sh | bash
```

This installs to `/media/fat/Scripts/.trove/` and drops a `trove.sh` entry into MiSTer's Scripts menu.

## Pair with RomM

1. In RomM's web UI: **Control Panel → API Keys → Pair Device** (generates an 8-character code, valid for 60 seconds).
2. On the MiSTer:

    ```bash
    /media/fat/Scripts/.trove/bin/trove pair
    ```

   Enter the RomM URL and the pairing code. Trove stores the resulting bearer token in its config.

## Subscribe to a collection

Open the config:

```bash
/media/fat/Scripts/.trove/bin/trove config
```

Add your collection IDs under `subscriptions.collections`. You can find these in RomM's URL when viewing a collection.

## First sync

Preview what would happen without changing anything:

```bash
/media/fat/Scripts/.trove/bin/trove sync --dry-run
```

When happy:

```bash
/media/fat/Scripts/.trove/bin/trove sync
```

## Command reference

| Command | What it does |
|---|---|
| `trove pair` | Pair with a RomM instance (URL + short code) |
| `trove sync` | Run a sync now (`--dry-run` to preview) |
| `trove bios <slug>` | Download BIOS for one platform (or `all` for every subscribed one) |
| `trove status` | Show install + subscription summary |
| `trove config` | Edit `config.json` in `$EDITOR` (falls back to `vi`) |
| `trove doctor` | Sanity-check RomM reachability, paths, cron, disk |
| `trove update [--check]` | Update from GitHub Releases |

All commands accept `-v` (verbose) and `-q` (quiet — file log still writes).

## Config reference

Trove's config lives at `/media/fat/Scripts/.trove/config.json`. A commented example is available at [`config.example.json`](config.example.json). Key fields:

- **`romm.url`** — your RomM instance URL. Set by `trove pair`.
- **`romm.token`** — bearer token from pairing. Do not edit by hand.
- **`mister.root`** — where MiSTer stores games. Defaults to `/media/fat`; change to `/media/usb0` or `/media/network` if your library lives there.
- **`subscriptions.collections`** — array of RomM collection IDs to sync.
- **`sync.saves_and_states`** — enable bidirectional save/state sync. Default: `true`.
- **`sync.on_rom_conflict`** — what to do when a ROM exists on both sides with different sizes. One of `romm_wins` / `mister_wins` / `skip`. Default: `romm_wins`.
- **`sync.on_asset_conflict`** — what to do when a save/state differs and timestamps are ambiguous. One of `newest_wins` / `romm_wins` / `mister_wins` / `skip`. Default: `newest_wins`.
- **`sync.on_orphan`** — what to do with files Trove previously downloaded that are no longer in any subscription. One of `keep` / `delete`. Default: `keep`.
- **`sync.emulator_filter`** — `mister_only` skips saves/states tagged for other emulators (RetroArch, fceux, dolphin, …). `allow_all` pulls everything. Default: `mister_only`.
- **`cron.schedule`** — cron-format schedule for auto-sync. Default: `0 3 * * *` (3 AM daily).
- **`cron.enabled`** — informational; the installer manages the actual cron entry.
- **`logging.level`** — `DEBUG` / `INFO` / `WARNING` / `ERROR`. Default: `INFO`.
- **`core_overrides`** — per-slug MiSTer folder overrides for non-standard setups. Example: `{"genesis": "Genesis"}` if your Sega Mega Drive lives at `/media/fat/games/Genesis/` instead of the default `MegaDrive`.

## Where things live on the MiSTer

- Install root: `/media/fat/Scripts/.trove/`
- Config: `/media/fat/Scripts/.trove/config.json`
- Manifest (what Trove has placed): `/media/fat/Scripts/.trove/manifest.json`
- Logs: `/media/fat/Scripts/.trove/logs/trove.log` (rotated at 5 MB × 3)
- Downloaded ROMs: `/media/fat/games/<Core>/<file>`
- Downloaded saves: `/media/fat/saves/<Core>/<file>`
- Downloaded states: `/media/fat/savestates/<Core>/<file>`
- Downloaded BIOS: `/media/fat/games/<Core>/bios/<file>` (rename per your core's convention — see [MiSTer wiki](https://mister-devel.github.io/MkDocs_MiSTer/setup/games/))

## Uninstall

```bash
/media/fat/Scripts/.trove/uninstall.sh
```

By default this preserves `config.json`, `manifest.json`, and `logs/` so a re-install picks them up. Pass `--purge` to remove those too.

## Safety notes

Trove was designed for MiSTer users who have been curating their library for years. Two rules matter most:

1. **Orphan detection is manifest-scoped.** Trove remembers every file it has placed on the MiSTer (per-file, keyed by relative path, with `{mtime, size, hash}` metadata). Only files in that manifest can ever be considered for deletion. Pre-existing files you had before Trove are invisible to its cleanup logic — they will not be touched, ever, regardless of subscription state.
2. **Save uploads are gated on ROM management.** Trove will only upload a MiSTer-side save/state to RomM if the corresponding ROM is one Trove itself is managing. Pre-existing saves for pre-existing ROMs stay local.

If either of these ever misbehaves, please open an issue with your `trove.log`.

## License

MIT — see [LICENSE](LICENSE).
