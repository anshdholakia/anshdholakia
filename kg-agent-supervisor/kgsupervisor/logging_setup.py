"""Console + file logging."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO", logfile: str | None = None) -> logging.Logger:
    logger = logging.getLogger("kgs")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if logfile:
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s"))
        logger.addHandler(fh)

    return logger
