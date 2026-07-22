"""RomM platform slug → MiSTer core folder mapping.

MiSTer stores game ROMs under ``<mister_root>/games/<Core>/``. Core folder names
don't match RomM's slugs, so we bundle a translation table cross-checked against
a real MiSTer install (case matters — the SD's ext4 is case-sensitive).

Returns ``None`` for slugs with no known MiSTer core (Switch, PS3, Wii U, 3DS,
PSP, etc.). Users can override any mapping via ``core_overrides`` in their
config for non-standard setups.
"""
from __future__ import annotations

# Built-in slug→core mapping.
_SLUG_TO_CORE: dict[str, str] = {
    # Nintendo
    "nes":              "NES",
    "famicom":          "NES",
    "fds":              "NES",
    "snes":             "SNES",
    "sfc":              "SNES",
    "super-famicom":    "SNES",
    "n64":              "N64",
    "gb":               "GAMEBOY",
    "gbc":              "GBC",
    "gba":              "GBA",
    "sgb":              "SGB",
    "vb":               "VirtualBoy",
    "virtualboy":       "VirtualBoy",
    # Sega — MiSTer's core is "MegaDrive" (international), not the US "Genesis".
    "genesis-slash-megadrive": "MegaDrive",
    "genesis":          "MegaDrive",
    "megadrive":        "MegaDrive",
    "sms":              "SMS",
    "gg":               "GameGear",
    "gamegear":         "GameGear",
    "sg1000":           "SG-1000",
    "segacd":           "MegaCD",
    "sega-cd":          "MegaCD",
    "sega32x":          "S32X",
    "32x":              "S32X",
    "saturn":           "Saturn",
    # NEC
    "turbografx16-slash-pcengine": "TGFX16",
    "turbografx-16":    "TGFX16",
    "pcengine":         "TGFX16",
    "pce":              "TGFX16",
    "pcecd":            "TGFX16-CD",
    "turbografx-16-cd": "TGFX16-CD",
    # SNK
    "neogeoaes":        "NEOGEO",
    "neogeomvs":        "NEOGEO",
    "neogeocd":         "NeoGeo-CD",
    "neogeopocket":     "NeoGeoPocket",
    "ngp":              "NeoGeoPocket",
    "ngpc":             "NeoGeoPocket",
    # Sony
    "psx":              "PSX",
    "ps":               "PSX",
    "ps1":              "PSX",
    "playstation":      "PSX",
    # Atari
    "atari2600":        "Atari2600",
    "atari5200":        "ATARI5200",
    "atari7800":        "ATARI7800",
    "atari-lynx":       "AtariLynx",
    "lynx":             "AtariLynx",
    "atari8bit":        "ATARI800",
    "atari-8-bit":      "ATARI800",
    "jaguar":           "Jaguar",
    # Bandai
    "wonderswan":       "WonderSwan",
    "wonderswan-color": "WonderSwanColor",
    "wsc":              "WonderSwanColor",
    # Other consoles
    "colecovision":     "Coleco",
    "intellivision":    "Intellivision",
    "channel-f":        "ChannelF",
    "channelf":         "ChannelF",
    "odyssey--1":       "ODYSSEY2",
    "odyssey2":         "ODYSSEY2",
    "vectrex":          "VECTREX",
    "astrocade":        "Astrocade",
    "arcadia-2001":     "Arcadia",
    "arcadia":          "Arcadia",
    "megaduck":         "MegaDuck",
    "pokemon-mini":     "PokemonMini",
    "pokemonmini":      "PokemonMini",
    # Arcade
    "arcade":           "_Arcade",
    "mame":             "_Arcade",
    # Home computers
    "c64":              "C64",
    "commodore-c64":    "C64",
    "c128":             "C128",
    "vic-20":           "VIC20",
    "vic20":            "VIC20",
    "amiga":            "Amiga",
    "amstrad-cpc":      "Amstrad",
    "amstrad":          "Amstrad",
    "acorn-electron":   "AcornElectron",
    "bbc-micro":        "BBCMicro",
    "atari-st":         "AtariST",
    "atarist":          "AtariST",
    "msx":              "MSX",
    "msx2":             "MSX",
    "sinclair-zx81":    "ZX81",
    "zx81":             "ZX81",
    "sinclair-zx-spectrum": "Spectrum",
    "zxs":              "Spectrum",
    "zx-spectrum":      "Spectrum",
    "dos":              "AO486",
    "pc-dos":           "AO486",
    "win3x":            "AO486",
    "windows-3x":       "AO486",
    "coleco-adam":      "Adam",
    "adam":             "Adam",
    "trs-80":           "TRS-80",
}

# Slugs with no known MiSTer core — explicitly recorded so the sync log can
# surface "you have games for X platforms MiSTer can't run".
UNSUPPORTED_SLUGS: frozenset[str] = frozenset({
    "3do", "3do-interactive-multiplayer",
    "dc", "dreamcast",
    "gc", "ngc", "gamecube", "nintendo-gamecube",
    "wii", "nintendo-wii",
    "wiiu", "nintendo-wii-u",
    "switch", "nintendo-switch",
    "3ds", "nintendo-3ds", "new-nintendo-3ds",
    "nds", "nintendo-ds",
    "dsi", "nintendo-dsi",
    "ps2", "playstation-2",
    "ps3", "playstation-3",
    "ps4", "playstation-4",
    "ps5", "playstation-5",
    "psp", "playstation-portable",
    "psvita", "playstation-vita",
    "xbox",
    "xbox360", "xbox-360",
    "xboxone", "xbox-one",
    "series-x-s",
    "mac", "macintosh",
    "linux",
    "android",
    "ios",
    "browser",
    "mugen",
    "win",
    "windows",
})

# User overrides applied at runtime via ``apply_overrides``. Empty-value
# override explicitly disables a slug (returns None as unsupported).
_USER_OVERRIDES: dict[str, str] = {}


def apply_overrides(overrides: dict | None) -> None:
    """Replace the runtime override map. Call once at startup."""
    global _USER_OVERRIDES
    if not isinstance(overrides, dict):
        _USER_OVERRIDES = {}
        return
    _USER_OVERRIDES = {
        str(k).lower().strip(): str(v or "").strip()
        for k, v in overrides.items()
        if k
    }


def core_folder_for_slug(slug: str | None) -> str | None:
    """Return the MiSTer folder name under ``games/`` for a RomM slug, or None."""
    if not slug:
        return None
    key = slug.lower().strip()
    if key in _USER_OVERRIDES:
        return _USER_OVERRIDES[key] or None
    return _SLUG_TO_CORE.get(key)


def is_supported(slug: str | None) -> bool:
    return core_folder_for_slug(slug) is not None


def games_dir(slug: str, root: str = "/media/fat") -> str | None:
    core = core_folder_for_slug(slug)
    return f"{root}/games/{core}" if core else None


def saves_dir(slug: str, root: str = "/media/fat") -> str | None:
    core = core_folder_for_slug(slug)
    return f"{root}/saves/{core}" if core else None


def states_dir(slug: str, root: str = "/media/fat") -> str | None:
    core = core_folder_for_slug(slug)
    return f"{root}/savestates/{core}" if core else None
