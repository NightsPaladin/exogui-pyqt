"""
mac_shortcuts.py — Temporarily disable macOS keyboard shortcuts that conflict
with DOSBox gameplay.

Uses defaults export/import (via cfprefsd) to toggle specific shortcuts off
before a game launches and restore them when it exits.

Shortcuts suppressed while a game runs:
  Ctrl+Left / Ctrl+Right          — move between Spaces (IDs 79, 81)
  Ctrl+Shift+Left / +Right        — move window to adjacent Space (IDs 80, 82)
  Ctrl+Up / Ctrl+Down             — Mission Control / App Exposé (IDs 32, 33)
  Ctrl+1 … Ctrl+9                 — jump to Desktop 1–9 (IDs 118–126)

All F-key-based DOSBox controls (Ctrl+F1, Ctrl+F10, Ctrl+Enter, etc.) are
intentionally left untouched so DOSBox window management continues to work.
Cmd-based shortcuts (Cmd+Tab, Cmd+Space Spotlight, etc.) are also left alone
since DOS games do not use the Command key.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.platform != "darwin":
    def disable_conflicting_shortcuts() -> dict:
        return {}

    def restore_shortcuts(_saved: dict) -> None:
        return
else:
    _DOMAIN = "com.apple.symbolichotkeys"
    _ACTIVATE = (
        "/System/Library/PrivateFrameworks/SystemAdministration.framework"
        "/Resources/activateSettings"
    )

    # Symbolic hotkey IDs whose key combos conflict with standard DOS games.
    # Verified from a default macOS install (Ventura/Sonoma/Sequoia).
    # Value = default [unicodeKeyChar, virtualKeyCode, modifierFlags] used when
    # the entry is absent from the user's plist (system default = enabled).
    #   modifier 0x840000 (8650752) = Control  (arrow shortcuts)
    #   modifier 0x860000 (8781824) = Ctrl+Shift (arrow shortcuts)
    #   modifier 0x040000 (262144)  = Control  (number shortcuts)
    _CONFLICTING: dict[int, list[int] | None] = {
        # Ctrl+Up / Ctrl+Down — Mission Control / App Exposé
        32:  [65535, 126, 8650752],
        33:  [65535, 125, 8650752],
        # Ctrl+Left / Ctrl+Right — move between Spaces
        79:  [65535, 123, 8650752],
        80:  [65535, 123, 8781824],   # Ctrl+Shift+Left  (move window)
        81:  [65535, 124, 8650752],
        82:  [65535, 124, 8781824],   # Ctrl+Shift+Right (move window)
        # Ctrl+1 … Ctrl+9 — jump to Desktop N
        # key codes: 1→18, 2→19, 3→20, 4→21, 5→23, 6→22, 7→26, 8→28, 9→25
        118: [65535,  18, 262144],
        119: [65535,  19, 262144],
        120: [65535,  20, 262144],
        121: [65535,  21, 262144],
        122: [65535,  23, 262144],
        123: [65535,  22, 262144],
        124: [65535,  26, 262144],
        125: [65535,  28, 262144],
        126: [65535,  25, 262144],
    }

    def _read_hotkeys() -> dict:
        """Read AppleSymbolicHotKeys through cfprefsd (the live prefs daemon)."""
        with tempfile.NamedTemporaryFile(suffix=".plist", delete=False) as fh:
            tmp = fh.name
        try:
            subprocess.run(
                ["defaults", "export", _DOMAIN, tmp],
                check=True, capture_output=True,
            )
            with open(tmp, "rb") as fh:
                data = plistlib.load(fh)
            return data.get("AppleSymbolicHotKeys", {})
        finally:
            os.unlink(tmp)

    def _write_hotkeys(hotkeys: dict) -> None:
        """Write AppleSymbolicHotKeys back through cfprefsd, then notify the system."""
        with tempfile.NamedTemporaryFile(suffix=".plist", delete=False, mode="wb") as fh:
            plistlib.dump({"AppleSymbolicHotKeys": hotkeys}, fh, fmt=plistlib.FMT_XML)
            tmp = fh.name
        try:
            subprocess.run(
                ["defaults", "import", _DOMAIN, tmp],
                check=True, capture_output=True,
            )
        finally:
            os.unlink(tmp)
        subprocess.run([_ACTIVATE, "-u"], capture_output=True)
        # The Dock process owns Spaces/Mission Control shortcuts and must restart
        # to pick up the preference change. It relaunches itself automatically.
        subprocess.run(
            ["osascript", "-e", 'tell application "Dock" to quit'],
            capture_output=True,
        )

    def disable_conflicting_shortcuts() -> dict:
        """
        Disable macOS shortcuts that conflict with DOSBox.

        Returns a snapshot so restore_shortcuts() can put things back exactly
        as they were.  Returns {} on any error (safe to pass to restore_shortcuts).
        """
        try:
            hotkeys = _read_hotkeys()
            snapshot: dict = {}

            for kid, default_params in _CONFLICTING.items():
                key = str(kid)
                if key in hotkeys:
                    snapshot[key] = dict(hotkeys[key])
                    hotkeys[key]["enabled"] = False
                elif default_params is not None:
                    # Absent entry = system default (enabled). Insert a disabled
                    # entry with known parameters so macOS honours the override.
                    snapshot[key] = None  # sentinel: entry didn't exist before
                    hotkeys[key] = {
                        "enabled": False,
                        "value": {"parameters": default_params, "type": "standard"},
                    }

            _write_hotkeys(hotkeys)
            return snapshot
        except Exception:
            return {}

    def restore_shortcuts(saved: dict) -> None:
        """
        Restore shortcuts to the state captured by disable_conflicting_shortcuts().
        Safe to call even if saved is empty or the plist changed on disk.
        """
        if not saved:
            return
        try:
            hotkeys = _read_hotkeys()

            for key, original in saved.items():
                if original is None:
                    # We created this entry (it was absent before). Since
                    # 'defaults import' only merges and never removes keys, we
                    # can't truly delete it — re-enable it so macOS treats it
                    # as active again, which matches the pre-game state.
                    if key in hotkeys:
                        hotkeys[key]["enabled"] = True
                else:
                    hotkeys[key] = original

            _write_hotkeys(hotkeys)
        except Exception:
            pass
