#!/usr/bin/env python3
"""
main.py — Entry point for exogui-pyqt (unified macOS + Linux GUI).

Usage:
    python3 main.py [/path/to/eXo-collection]

The collection root defaults to the parent directory of this script's
directory, i.e. the mounted eXo volume root.

One deliberate platform-specific workaround lives here: when the GUI is run as
plain ``python3 main.py`` instead of from a packaged app bundle, Qt alone
cannot consistently rename the app/process from "python3" to "eXoGUI" across
macOS and Linux.  The title/process-name helpers below are kept together and
documented so that workaround remains explicit rather than feeling accidental.
"""

import os
import sys


def _consume_flag(argv: list[str], *flags: str) -> tuple[list[str], bool]:
    """Return ``(argv_without_flags, found)`` for command-line flags we handle."""
    matched = {flag for flag in flags if flag in argv}
    if not matched:
        return argv, False
    return [arg for arg in argv if arg not in matched], True


def _set_app_process_name(name: str) -> None:
    """
    Set a friendly process/app name so the app appears as 'eXoGUI' rather
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
      3. Auto-detect: parent directory of this script if it contains a known eXo marker
    """
    if len(argv) > 1 and os.path.isdir(argv[1]):
        return os.path.abspath(argv[1])

    env = os.environ.get("EXODOS_ROOT", "")
    if env and os.path.isdir(env):
        return env

    # Auto-detect: this script lives in <collection-root>/exogui-pyqt/
    from core.project import detect_project
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.dirname(here)   # one level up = collection root
    if detect_project(candidate) is not None:
        return candidate

    return ""


def _install_qt_message_handler() -> None:
    """
    Suppress Qt's verbose internal logging unless --debug is active.

    Qt prints debug-level category messages to stderr by default (e.g.
    "qt.multimedia.ffmpeg: Using Qt multimedia with FFmpeg…").  In normal
    mode we drop debug/info messages entirely and only surface warnings or
    above — minus the known-harmless Qt accessibility table warning that
    fires on every model reset in QTreeView on macOS.  In debug mode every
    Qt message is forwarded to stderr via the standard [DEBUG] prefix.
    """
    from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
    import core.debug as _dbg

    _QT_SHOW = {QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg}

    def _handler(msg_type: QtMsgType, context, message: str) -> None:
        # Qt passes the bare message text; the "qt.accessibility.table:" prefix
        # seen on stderr is added by Qt's *default* handler, not present in
        # `message` itself.  The category lives on `context.category` instead.
        cat = getattr(context, 'category', '') or ''
        if _dbg.DEBUG:
            prefix = f"[Qt/{cat}]" if cat else "[Qt]"
            print(f"{prefix} {message}", file=sys.stderr)
        elif msg_type in _QT_SHOW and not cat.startswith('qt.accessibility'):
            print(message, file=sys.stderr)

    qInstallMessageHandler(_handler)


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
    # Enable debug logging if --debug is on the command line (strip it before
    # passing argv to QApplication so Qt doesn't emit unknown-option warnings).
    sys.argv, debug_enabled = _consume_flag(sys.argv, "--debug", "-debug")
    if debug_enabled:
        import core.debug as _debug_mod
        _debug_mod.DEBUG = True
        print("[DEBUG] Debug logging enabled", file=sys.stderr)

    sys.argv, _reset_pin = _consume_flag(sys.argv, "--reset-pin")

    root = find_project_root(sys.argv)

    # On Linux: ensure Qt's bundled libpulse can reach PipeWire before Qt init
    _setup_linux_audio()

    # Import Qt here so errors are more readable if PyQt6 is missing
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QTimer
    except ImportError:
        print("ERROR: PyQt6 is not installed.", file=sys.stderr)
        print("  Install it with:  pip3 install PyQt6", file=sys.stderr)
        sys.exit(1)

    # Gate Qt's own internal logging (qt.multimedia.ffmpeg, qt.accessibility…)
    # behind --debug; suppress harmless noise in normal mode.
    _install_qt_message_handler()

    from gui.main_window import MainWindow, APP_NAME, APP_VERSION
    from gui.app_icon import make_app_icon

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("eXoGUI")
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(make_app_icon())

    # Keep the process/menu title workaround explicit: this is the one
    # intentional platform hack in the app, needed for plain-script launches.
    # Set process name now that NSApplication is initialised.
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

    if _reset_pin:
        from PyQt6.QtCore import QSettings
        from gui.pin_dialog import clear_pin
        _s = QSettings(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "exogui.ini"),
            QSettings.Format.IniFormat,
        )
        clear_pin(_s)
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.information(
            None, "PIN Reset",
            "The parental control PIN has been cleared.\n"
            "You will be prompted to set a new one on next launch.",
        )

    window = MainWindow(root)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
