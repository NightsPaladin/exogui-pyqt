#!/usr/bin/env bash
# exogui.command — Launcher for the eXo PyQt6 GUI.
# Works on both macOS (double-click in Finder) and Linux (bash/terminal).
# Supports eXoDOS, eXoWin3x, and any future eXo projects.
# Project roots are stored in settings — configure them via File > Settings.

if [[ "$LD_PRELOAD" =~ "gameoverlayrenderer" ]]; then
    LD_PRELOAD=""
fi

cd "$( dirname "$BASH_SOURCE" )"
SCRIPT_DIR="$(pwd)/exogui-pyqt"

# ── Dependency checks ─────────────────────────────────────────────────────────

check_python() {
    local py
    py=$(command -v python3 2>/dev/null || true)
    if [[ -z "$py" ]]; then
        echo "ERROR: python3 not found."
        if [[ "$OSTYPE" == "darwin"* ]]; then
            echo "  Install it with: brew install python"
        else
            echo "  Install it with your package manager (e.g. sudo apt install python3)"
        fi
        exit 1
    fi
    echo "$py"
}

check_pyqt6() {
    python3 -c "import PyQt6" 2>/dev/null && return 0 || return 1
}

install_pyqt6() {
    echo ""
    echo "PyQt6 is not installed. Installing now…"
    if [[ "$OSTYPE" == "linux-gnu"* ]] && command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3-pyqt6 2>/dev/null || \
            python3 -m pip install --user PyQt6 --quiet
    else
        python3 -m pip install PyQt6 --quiet
    fi
}

check_pyobjc() {
    python3 -c "from AppKit import NSApplication" 2>/dev/null && return 0 || return 1
}

install_pyobjc() {
    echo ""
    echo "pyobjc-framework-Cocoa is not installed. Installing now…"
    python3 -m pip install pyobjc-framework-Cocoa --quiet
}

# ── Main ──────────────────────────────────────────────────────────────────────

echo "════════════════════════════════════════"
echo "  eXo GUI"
echo "════════════════════════════════════════"
echo ""

PYTHON=$(check_python)
echo "Python: $PYTHON"

if ! check_pyqt6; then
    install_pyqt6
fi

# macOS: pyobjc needed to display correct app name in the menu bar
if [[ "$OSTYPE" == "darwin"* ]] && ! check_pyobjc; then
    install_pyobjc
fi

echo "Starting GUI…"
echo ""

cd "$SCRIPT_DIR"
exec "$PYTHON" main.py
