"""
debug.py — Global debug flag and logging helper.

Enable with the --debug CLI flag:
    python3 main.py --debug

All dbg() calls are no-ops when DEBUG is False (the default).
"""

from __future__ import annotations

import sys

DEBUG: bool = False


def dbg(*args, **kwargs) -> None:
    """Print a debug message to stderr when DEBUG is True."""
    if DEBUG:
        print("[DEBUG]", *args, file=sys.stderr, **kwargs)
