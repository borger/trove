"""Trove — retro library manager for MiSTer FPGA."""
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent.parent / "VERSION"
try:
    __version__ = VERSION_FILE.read_text().strip()
except OSError:
    __version__ = "0.0.0-dev"
