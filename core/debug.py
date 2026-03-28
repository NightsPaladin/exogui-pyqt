"""
debug.py — Global debug-mode flag.

Set to True by passing -d / --debug on the command line (handled in main.py).
Import this wherever conditional debug output is needed:

    from core import debug
    if debug.enabled:
        print("[component] ...", file=sys.stderr)
"""

enabled: bool = False
