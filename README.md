# Trove

Headless MiSTer companion that keeps your MiSTer FPGA library in sync with a self-hosted [RomM](https://github.com/rommapp/romm) server. Ships as a single-file installer, uses only Python's standard library, and physically cannot delete files it didn't place — safe to run against a library you've spent years curating.

## Features

- **Pair once, sync forever** — one 60-second pairing code from RomM's web UI. No passwords stored.
- **Interactive collection picker** — `trove collections` opens a multi-select TUI; no JSON editing.
- **Two-way saves & states** — MiSTer-side saves upload to RomM, RomM edits pull down. Newest-wins reconciliation with MD5 verification.
- **BIOS/firmware download** — per platform, or every subscribed platform in one shot.
- **Live download progress** — bytes transferred, percent, speed, and ETA for anything larger than 5 MB. Instant completion on any size.
- **Scheduled auto-sync** — one command sets up BusyBox `crond` at boot and registers the schedule.
- **Self-update** — `trove update` pulls the latest release from GitHub.
- **Doctor** — `trove doctor` reports RomM reachability, MiSTer paths, cron state, and disk space in one shot.
- **Runs from the MiSTer Scripts menu** — controller-friendly whiptail menu, no terminal required.

## Safety by design

Trove was built for MiSTer users with libraries they've curated over years. Two architectural guarantees:

1. **Orphan detection is manifest-scoped.** Trove records every file it places on the MiSTer, keyed by relative path with `{mtime, size, hash}` metadata. Only files present in that manifest can ever be considered for deletion. Files you had before installing Trove are invisible to the cleanup logic — they will not be touched, ever, regardless of what you subscribe to or unsubscribe from.
2. **Save uploads are gated on ROM management.** Trove uploads a MiSTer-side save/state to RomM only when the corresponding ROM is one Trove itself is managing. Pre-existing saves for pre-existing ROMs stay local.

If either of these ever misbehaves, please open an issue with your `trove.log` attached.

## Requirements

- MiSTer FPGA with Python 3 available (present on any standard MiSTer install with `update_all`).
- A running [RomM](https://github.com/rommapp/romm) instance (v5.0.0 or later) reachable from the MiSTer's network.

No `pip`, no external Python packages, no Docker.

## Install

SSH into your MiSTer (or open a shell via the Scripts menu) and run:

```bash
curl -kL https://raw.githubusercontent.com/borger/trove/main/install.sh | bash
```

This installs to `/media/fat/Scripts/.trove/` and drops a `trove.sh` entry into MiSTer's Scripts menu so you can drive everything with a controller.

To set up scheduled auto-sync at the same time (starts crond now, adds it to `user-startup.sh`, registers the schedule):

```bash
curl -kL https://raw.githubusercontent.com/borger/trove/main/install.sh | bash -s -- --enable-cron
```

## First-run

Three commands from a fresh install:

### 1. Pair with RomM

In RomM's web UI: **Control Panel → API Keys → Pair Device**. You'll get an 8-character code, valid for 60 seconds.

On the MiSTer:

```bash
/media/fat/Scripts/.trove/bin/trove pair
```

Enter the RomM URL when prompted, paste the pairing code.

### 2. Subscribe to collections

```bash
/media/fat/Scripts/.trove/bin/trove collections
```

Opens an interactive picker: **Space** to toggle, **Enter** to save, **Esc** to cancel. All your RomM collections appear with ROM counts, sorted by size so the meaty ones surface first.

### 3. Sync

Preview first — recommended on the first run so you can eyeball the plan:

```bash
/media/fat/Scripts/.trove/bin/trove sync --dry-run
```

Then, for real:

```bash
/media/fat/Scripts/.trove/bin/trove sync
```

## Auto-sync (BusyBox cron)

MiSTer ships `crond` (via BusyBox) but doesn't start it by default. Enabling scheduled sync is a single command:

```bash
/media/fat/Scripts/.trove/bin/trove enable-cron
```

This starts crond immediately AND appends `/usr/sbin/crond -b` to `/media/fat/linux/user-startup.sh` so it runs at every boot. Trove's schedule (`0 3 * * *` — daily at 3 AM by default) is already registered.

To pause auto-sync without uninstalling:

```bash
/media/fat/Scripts/.trove/bin/trove disable-cron
```

Stops crond, removes the boot line. The schedule file stays in place so `trove enable-cron` resumes cleanly.

To change the schedule: edit `cron.schedule` in `config.json`, then re-run `trove enable-cron`.

## From the MiSTer Scripts menu

Everything above is available without a terminal — from the MiSTer's Scripts menu, pick **trove.sh** and you get a whiptail menu with:

- **status** / **pair** / **collections**
- **sync** (and **sync-dry** for a preview)
- **bios-all** (download BIOS for every subscribed platform)
- **config** / **doctor**
- **enable-cron** / **disable-cron**
- **update** / **logs**

Full controller navigation, no keyboard needed.

## Command reference

| Command | What it does |
|---|---|
| `trove pair [--url URL] [--code CODE]` | Pair with a RomM instance |
| `trove collections` | Interactive picker for subscribed collections |
| `trove collections list` | Print subscribed + available collections |
| `trove collections add <ids…>` | Subscribe to collection IDs |
| `trove collections remove <ids…>` | Unsubscribe from collection IDs |
| `trove sync [--dry-run]` | Run sync (or preview) |
| `trove bios <slug\|all>` | Download BIOS for one platform, or every subscribed one |
| `trove status` | Show install + subscription summary |
| `trove config` | Edit `config.json` in `$EDITOR` |
| `trove doctor` | Sanity-check RomM, MiSTer paths, cron, disk |
| `trove enable-cron` | Start crond now + persist to `user-startup.sh` |
| `trove disable-cron` | Stop crond + remove `user-startup.sh` line |
| `trove update [--check]` | Update from GitHub Releases |

All commands accept `-v` (DEBUG logging) and `-q` (silence console; the file log still writes).

## Config reference

Config lives at `/media/fat/Scripts/.trove/config.json`. Example with sensible defaults: [`config.example.json`](config.example.json).

- **`romm.url`** — RomM URL. Populated by `trove pair`.
- **`romm.token`** — Bearer token. Populated by `trove pair`. Do not edit by hand.
- **`mister.root`** — Where MiSTer keeps games. Default `/media/fat`; change to `/media/usb0` or `/media/network` if your library lives there.
- **`subscriptions.collections`** — Array of collection IDs to sync. Managed by `trove collections`.
- **`sync.saves_and_states`** — Enable bidirectional save/state sync. Default: `true`.
- **`sync.on_rom_conflict`** — When a ROM exists on both sides with different sizes: `romm_wins` / `mister_wins` / `skip`. Default: `romm_wins`.
- **`sync.on_asset_conflict`** — When a save/state differs and timestamps are ambiguous: `newest_wins` / `romm_wins` / `mister_wins` / `skip`. Default: `newest_wins`.
- **`sync.on_orphan`** — When Trove-placed files no longer belong to any subscription: `keep` / `delete`. Default: `keep`.
- **`sync.emulator_filter`** — `mister_only` skips saves/states tagged for other emulators (RetroArch, fceux, dolphin, …); `allow_all` pulls everything. Default: `mister_only`.
- **`cron.schedule`** — Cron-format schedule for auto-sync. Default: `0 3 * * *` (daily at 3 AM).
- **`logging.level`** — `DEBUG` / `INFO` / `WARNING` / `ERROR`. Default: `INFO`.
- **`core_overrides`** — Per-slug MiSTer folder overrides for non-standard setups. Example: `{"genesis": "Genesis"}` if your Sega Mega Drive folder is `/media/fat/games/Genesis/` instead of the default `MegaDrive`.

## Where things live

| Path | What |
|---|---|
| `/media/fat/Scripts/.trove/` | Install root |
| `/media/fat/Scripts/.trove/config.json` | Configuration |
| `/media/fat/Scripts/.trove/manifest.json` | Placement records (mtime + size + hash) |
| `/media/fat/Scripts/.trove/logs/trove.log` | Rotating log (5 MB × 3) |
| `/media/fat/Scripts/trove.sh` | Symlink into the Scripts menu |
| `/var/spool/cron/crontabs/root` | BusyBox cron entry (installed by Trove) |
| `/media/fat/games/<Core>/<file>` | Downloaded ROMs |
| `/media/fat/saves/<Core>/<file>` | Downloaded saves |
| `/media/fat/savestates/<Core>/<file>` | Downloaded states |
| `/media/fat/games/<Core>/bios/<file>` | Downloaded BIOS (see the [MiSTer wiki](https://mister-devel.github.io/MkDocs_MiSTer/setup/games/) for per-core naming conventions) |

## Update

```bash
/media/fat/Scripts/.trove/bin/trove update
```

Preserves your config, manifest, and logs across upgrades. Or just re-run `install.sh` — same result.

## Uninstall

```bash
/media/fat/Scripts/.trove/uninstall.sh
```

Preserves `config.json`, `manifest.json`, and `logs/` by default so a re-install picks them up. Pass `--purge` to remove everything.

## License

MIT — see [LICENSE](LICENSE).
