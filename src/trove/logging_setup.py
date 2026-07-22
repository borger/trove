"""Rotating logger for Trove.

- Rotating file handler at ``<TROVE_HOME>/logs/trove.log`` (5 MB × 3 by default).
- Console handler with concise formatting (level: INFO in cron mode, DEBUG with -v).
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import config


def setup(level: str = "INFO", *, quiet: bool = False) -> logging.Logger:
    cfg = config.load()
    log_cfg = cfg.get("logging") or {}
    max_bytes = int(log_cfg.get("max_bytes") or 5_242_880)
    backups = int(log_cfg.get("backup_count") or 3)

    logger = logging.getLogger("trove")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    logs_dir = config.logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_h = RotatingFileHandler(
        logs_dir / "trove.log",
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    file_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    logger.addHandler(file_h)

    if not quiet:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console)

    return logger
