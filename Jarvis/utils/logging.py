"""Console logging. The #logs Discord handler is Phase 9."""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger. basicConfig is a no-op after the first call."""
    # ponytail: LOG_LEVEL is read here rather than via Config because logging must work
    # before (and during) config validation. It is the only os.getenv outside config.py.
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO", format=_FORMAT)
    return logging.getLogger(name)
