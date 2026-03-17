"""
themes.py — Application theme system.

Themes are loaded from *.json files in  <repo>/themes/
Users can add their own themes by dropping a JSON file in that directory.

Usage
-----
    from gui.themes import current, set_theme, THEME_NAMES

    t = current()           # ThemeColors for the active theme
    set_theme("Dark", app)  # switch + apply palette/QSS to QApplication
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication


# ── theme directory ───────────────────────────────────────────────────────────

# Built-in themes live in  <project-root>/themes/  (sibling of gui/)
# Users add custom themes to the same directory.
THEMES_DIR = Path(__file__).parent.parent / "themes"

# Preferred display order for the menu.
# Any theme whose name isn't listed here is appended alphabetically at the end.
_BUILTIN_ORDER = (
    "Dark", "Light",
    "Rose Pine", "Cyberpunk",
    "Tokyo Night", "Dracula", "Nord", "Catppuccin",
    "Gruvbox", "Monokai", "Everforest", "Matrix",
    "Ocean", "One Dark",
    "Ayu Dark", "Kanagawa",
    "Solarized Dark", "Solarized Light",
)


# ── color set ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ThemeColors:
    name: str

    # Backgrounds
    bg_window:  str   # main window / deepest layer
    bg_panel:   str   # panel / scroll area background
    bg_card:    str   # alternate rows / cards
    bg_input:   str   # text inputs / combo boxes
    bg_status:  str   # status bar / menu bar

    # Borders & chrome
    border:     str   # default widget border
    handle:     str   # splitter / divider

    # Accent
    accent:     str   # selection / highlight / primary button

    # Text
    text_hi:    str   # primary text
    text_med:   str   # secondary / caption text
    text_lo:    str   # dim / tertiary text

    # Semantic
    green:      str   # installed indicator
    orange:     str   # warning / emulator badge

    # ── constructors ─────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict) -> "ThemeColors":
        valid = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in d.items() if k in valid}
        missing = valid - set(filtered)
        if missing:
            raise ValueError(f"Theme '{d.get('name', '?')}' missing fields: {missing}")
        return cls(**filtered)

    # ── derived helpers ───────────────────────────────────────────────────────

    def palette(self) -> QPalette:
        R = QPalette.ColorRole
        G = QPalette.ColorGroup
        p = QPalette()
        p.setColor(R.Window,          QColor(self.bg_window))
        p.setColor(R.WindowText,      QColor(self.text_hi))
        p.setColor(R.Base,            QColor(self.bg_panel))
        p.setColor(R.AlternateBase,   QColor(self.bg_card))
        p.setColor(R.Text,            QColor(self.text_hi))
        p.setColor(R.Button,          QColor(self.bg_card))
        p.setColor(R.ButtonText,      QColor(self.text_hi))
        p.setColor(R.Highlight,       QColor(self.accent))
        p.setColor(R.HighlightedText, QColor("#ffffff"))
        p.setColor(R.ToolTipBase,     QColor(self.bg_input))
        p.setColor(R.ToolTipText,     QColor(self.text_hi))
        p.setColor(R.Mid,             QColor(self.text_lo))
        p.setColor(R.Shadow,          QColor(self.text_lo))
        p.setColor(R.BrightText,      QColor("#ffffff"))
        p.setColor(R.Link,            QColor(self.accent))
        p.setColor(R.PlaceholderText, QColor(self.text_med))
        p.setColor(G.Disabled, R.Text,       QColor(self.text_lo))
        p.setColor(G.Disabled, R.ButtonText, QColor(self.text_lo))
        p.setColor(G.Disabled, R.WindowText, QColor(self.text_lo))
        return p

    def qss(self) -> str:
        """App-level stylesheet covering menus, dialogs, and chrome elements."""
        a, t = self.accent, self
        return f"""
QMainWindow, QDialog {{
    background: {t.bg_window};
}}
QMenuBar {{
    background: {t.bg_status};
    color: {t.text_hi};
    border-bottom: 1px solid {t.border};
}}
QMenuBar::item {{
    padding: 4px 8px;
    background: transparent;
}}
QMenuBar::item:selected {{
    background: {a};
    color: #ffffff;
    border-radius: 3px;
}}
QMenu {{
    background: {t.bg_panel};
    color: {t.text_hi};
    border: 1px solid {t.border};
    padding: 2px;
}}
QMenu::item {{
    padding: 5px 22px 5px 22px;
}}
QMenu::item:selected {{
    background: {a};
    color: #ffffff;
    border-radius: 3px;
}}
QMenu::separator {{
    height: 1px;
    background: {t.border};
    margin: 3px 4px;
}}
QMenu::indicator {{
    width: 14px;
    height: 14px;
}}
QToolTip {{
    background: {t.bg_card};
    color: {t.text_hi};
    border: 1px solid {t.border};
    padding: 3px 6px;
}}
QStatusBar {{
    background: {t.bg_status};
    color: {t.text_lo};
    font-size: 11px;
}}
QStatusBar QLabel {{
    color: {t.text_lo};
    font-size: 11px;
}}
QProgressBar {{
    background: {t.bg_input};
    border: 1px solid {t.border};
    border-radius: 4px;
    text-align: center;
    color: {t.text_hi};
}}
QProgressBar::chunk {{
    background: {a};
    border-radius: 4px;
}}
QSplitter::handle {{
    background: {t.handle};
}}
QFormLayout QLabel {{
    color: {t.text_hi};
}}
"""


# ── loader ────────────────────────────────────────────────────────────────────

def _load_themes() -> dict[str, ThemeColors]:
    """Scan THEMES_DIR for *.json files and return name → ThemeColors map."""
    result: dict[str, ThemeColors] = {}
    if not THEMES_DIR.exists():
        print(f"[themes] themes directory not found: {THEMES_DIR}", file=sys.stderr)
        return result
    for path in sorted(THEMES_DIR.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            t = ThemeColors.from_dict(data)
            result[t.name] = t
        except Exception as exc:
            print(f"[themes] Skipping {path.name}: {exc}", file=sys.stderr)
    return result


def _build_theme_names(themes: dict[str, ThemeColors]) -> tuple[str, ...]:
    """System first, then builtin order, then any user themes alphabetically."""
    names: list[str] = ["System"]
    for n in _BUILTIN_ORDER:
        if n in themes:
            names.append(n)
    known = set(_BUILTIN_ORDER)
    for n in sorted(themes):
        if n not in known:
            names.append(n)
    return tuple(names)


# ── emergency inline fallbacks (used when themes dir is missing/empty) ────────

_DARK_FALLBACK = ThemeColors(
    name="Dark",
    bg_window="#1c1c1e", bg_panel="#252523", bg_card="#323230",
    bg_input="#2c2c2a", bg_status="#141412",
    border="#48484a", handle="#3a3a38", accent="#4a90d9",
    text_hi="#f2f2f0", text_med="#8e8e8a", text_lo="#636360",
    green="#30d158", orange="#ff9f0a",
)
_LIGHT_FALLBACK = ThemeColors(
    name="Light",
    bg_window="#f0f0f5", bg_panel="#ffffff", bg_card="#eaeaf2",
    bg_input="#ffffff", bg_status="#dcdce8",
    border="#cccccc", handle="#ccccdd", accent="#4a90d9",
    text_hi="#1a1a2a", text_med="#555566", text_lo="#888899",
    green="#2e7d32", orange="#c45200",
)


# ── module-level state ────────────────────────────────────────────────────────

_THEMES: dict[str, ThemeColors] = _load_themes()

# Ensure we always have at least Dark and Light
if "Dark" not in _THEMES:
    _THEMES["Dark"] = _DARK_FALLBACK
if "Light" not in _THEMES:
    _THEMES["Light"] = _LIGHT_FALLBACK

THEME_NAMES: tuple[str, ...] = _build_theme_names(_THEMES)

_current: ThemeColors = _THEMES.get("Dark", _DARK_FALLBACK)
_current_name: str = "Dark"


# ── public API ────────────────────────────────────────────────────────────────

def current() -> ThemeColors:
    """Return the active ThemeColors object."""
    return _current


def current_name() -> str:
    """Return the active theme name (e.g. 'Dark', 'System', 'Rose Pine')."""
    return _current_name


def reload() -> None:
    """Re-scan the themes directory (picks up newly added JSON files)."""
    global _THEMES, THEME_NAMES
    _THEMES = _load_themes()
    if "Dark"  not in _THEMES: _THEMES["Dark"]  = _DARK_FALLBACK
    if "Light" not in _THEMES: _THEMES["Light"] = _LIGHT_FALLBACK
    THEME_NAMES = _build_theme_names(_THEMES)


# ── OS dark-mode detection ────────────────────────────────────────────────────

def detect_system_dark() -> bool:
    """Return True if the OS/desktop environment prefers a dark colour scheme."""
    if sys.platform == "darwin":
        import subprocess
        try:
            r = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=2,
            )
            return r.stdout.strip().lower() == "dark"
        except Exception:
            pass

    if sys.platform.startswith("linux"):
        import subprocess
        try:
            r = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True, timeout=2,
            )
            if "dark" in r.stdout.lower():
                return True
        except Exception:
            pass
        if "dark" in os.environ.get("GTK_THEME", "").lower():
            return True
        try:
            from PyQt6.QtGui import QGuiApplication
            hints = QGuiApplication.styleHints()
            if hasattr(hints, "colorScheme"):
                from PyQt6.QtCore import Qt
                return hints.colorScheme() == Qt.ColorScheme.Dark
        except Exception:
            pass

    pal = QApplication.palette()
    return pal.color(QPalette.ColorRole.Window).lightness() < 128


# ── apply ─────────────────────────────────────────────────────────────────────

def set_theme(name: str, app: QApplication) -> ThemeColors:
    """
    Switch to *name*, apply palette + QSS to *app*, update the singleton,
    and return the resolved ThemeColors.
    """
    global _current, _current_name
    _current_name = name
    if name == "System":
        resolved = _THEMES.get("Dark" if detect_system_dark() else "Light",
                                _DARK_FALLBACK)
    else:
        resolved = _THEMES.get(name, _DARK_FALLBACK)
    _current = resolved
    app.setPalette(resolved.palette())
    app.setStyleSheet(resolved.qss())
    return resolved
