#!/usr/bin/env python3
"""
main.py — Entry point for exogui-pyqt (unified macOS + Linux GUI).

Usage:
    python3 main.py [-d | --debug] [/path/to/eXoDOS]

The eXoDOS root defaults to the parent directory of this script's directory,
i.e. the mounted eXoDOS volume root.

Flags:
    -d, --debug     Enable diagnostic output (audio device info, theme errors, etc.)
"""

import os
import sys


def _set_app_process_name(name: str) -> None:
    """
    Set a friendly process/app name so the app appears as 'eXoDOS' rather
    than 'python3.x' in CMD+TAB (macOS), the taskbar, or process listings.

    macOS: sets CFBundleName via PyObjC, falls back to ctypes setprogname.
    Linux: sets the process title via /proc/self/comm (best-effort).
    """
    if sys.platform == "darwin":
        try:
            from Foundation import NSBundle  # type: ignore[import]
            info = NSBundle.mainBundle().infoDictionary()
            if info is not None:
                info["CFBundleName"]             = name
                info["CFBundleDisplayName"]      = name
                info["NSHumanReadableShortName"] = name
            return
        except Exception:
            pass
        try:
            import ctypes
            libc = ctypes.CDLL(None)
            libc.setprogname(name.encode())
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            # Write to /proc/self/comm (max 15 chars, kernel truncates silently)
            with open("/proc/self/comm", "w") as fh:
                fh.write(name[:15])
        except Exception:
            pass


def find_project_root(argv: list[str]) -> str:
    """
    Resolve a fallback project root (used only for first-run settings migration).
      1. Command-line argument
      2. EXODOS_ROOT environment variable
      3. Auto-detect: walk up from this file looking for a known eXo marker
    """
    if len(argv) > 1 and os.path.isdir(argv[1]):
        return os.path.abspath(argv[1])

    env = os.environ.get("EXODOS_ROOT", "")
    if env and os.path.isdir(env):
        return env

    # Auto-detect: this script lives in eXoDOS/exogui-pyqt/
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.dirname(here)   # one level up = project root
    for marker in ("xml/all/MS-DOS.xml", "xml/Windows 3x.xml", "eXo/eXoDOS", "eXo/eXoWin3x"):
        if os.path.exists(os.path.join(candidate, *marker.split("/"))):
            return candidate

    return ""


def _fix_macos_menu_name(name: str) -> None:
    """
    Rename the macOS Application menu bar entry from 'python3' to the app name.
    Must be called after the QApplication event loop has started (e.g. via QTimer).
    """
    try:
        from AppKit import NSApplication  # type: ignore[import]
        ns_app = NSApplication.sharedApplication()
        menu = ns_app.mainMenu()
        if menu and menu.numberOfItems() > 0:
            menu.itemAtIndex_(0).setTitle_(name)
    except Exception:
        pass


def _setup_linux_audio() -> None:
    """
    Steer Qt6's PulseAudio output toward PipeWire's compatibility socket.

    pip-installed PyQt6 bundles Qt 6.x which uses libpulse for audio output on
    Linux.  libpulse auto-discovers the server via XDG_RUNTIME_DIR, but setting
    PULSE_SERVER explicitly avoids races and ensures the correct PipeWire-pulse
    socket is used before Qt initialises its audio subsystem.
    """
    if not sys.platform.startswith("linux"):
        return
    if "PULSE_SERVER" in os.environ:
        return
    try:
        xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        sock = os.path.join(xdg, "pulse", "native")
        if os.path.exists(sock):
            os.environ["PULSE_SERVER"] = f"unix:{sock}"
    except (AttributeError, OSError):
        pass


def main() -> None:
    import core.debug
    core.debug.enabled = "-d" in sys.argv or "--debug" in sys.argv

    root = find_project_root(sys.argv)

    # On Linux: ensure Qt's bundled libpulse can reach PipeWire before Qt init
    _setup_linux_audio()

    # Import Qt here so errors are more readable if PyQt6 is missing
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import Qt, QTimer
    except ImportError:
        print("ERROR: PyQt6 is not installed.", file=sys.stderr)
        print("  Install it with:  pip3 install PyQt6", file=sys.stderr)
        sys.exit(1)

    from gui.main_window import MainWindow, APP_NAME, APP_VERSION
    from gui.app_icon import make_app_icon

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("eXoGUI")
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(make_app_icon())

    # Set process name now that NSApplication is initialised
    _set_app_process_name(APP_NAME)

    # On macOS: rename the first menu bar item after the event loop starts.
    # Qt's setApplicationName() alone doesn't override the 'python3' menu title
    # when running as a plain script (no .app bundle).
    if sys.platform == "darwin":
        QTimer.singleShot(0, lambda: _fix_macos_menu_name(APP_NAME))

    # On Linux: tell the desktop environment which .desktop file we belong to
    # so the taskbar/dock shows the correct icon and title instead of "python3".
    if sys.platform.startswith("linux"):
        app.setDesktopFileName("exogui")

    window = MainWindow(root)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
