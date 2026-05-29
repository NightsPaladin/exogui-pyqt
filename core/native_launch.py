"""
native_launch.py — Build platform-native emulator argv for eXo collections.

Bypasses the shell-script dispatch chain (launch_helper.sh → launch.msh/bsh)
and constructs subprocess argv directly in Python, making the four primary
collections work on macOS and Linux without any patching.

Supported collection types
--------------------------
  exodos      — DOSBox Staging (or per-game override from dosbox_macos.txt)
  exowin3x    — DOSBox Staging (same pattern as eXoDOS)
  exoscummvm  — ScummVM (looks up engine:gameid from eXo/util/scummvm.txt)
  exoappleiigs — GSplus (MAME games not yet supported)

All DOSBox-based launches require CWD = collection_root/eXo/ because the
per-game dosbox_linux.conf autoexec sections use relative mounts such as:
    mount c ./eXoDOS/<gamedir>
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from typing import Optional


_SUPPORTED = frozenset(["exodos", "exowin3x", "exoscummvm", "exoappleiigs"])

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MACOS_MAP_PATH = os.path.join(_APP_DIR, "emulator_macos_map.txt")
_macos_map_cache: Optional[dict] = None


def _iter_text_lines(path: str) -> list[str]:
    """Return text lines from an optional support file, or ``[]`` on I/O failure."""
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return []


def _list_subdirs(path: str) -> list[str]:
    """Return sorted child directory names, or ``[]`` if the directory is unreadable."""
    try:
        return sorted(
            name for name in os.listdir(path)
            if os.path.isdir(os.path.join(path, name))
        )
    except OSError:
        return []


def _load_emulator_macos_map() -> dict[str, str]:
    """Parse emulator_macos_map.txt and return a {linux/win_cmd: macos_name} dict."""
    global _macos_map_cache
    if _macos_map_cache is not None:
        return _macos_map_cache
    result: dict[str, str] = {}
    for raw in _iter_text_lines(_MACOS_MAP_PATH):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and val:
            result[key] = val
    _macos_map_cache = result
    return result


def _map_to_macos(cmd: str) -> Optional[str]:
    """Translate a Linux/Windows emulator command to its macOS equivalent.

    Returns the macOS emulator name, or None if the command maps to
    "unavailable" or is not listed in emulator_macos_map.txt.
    """
    mapped = _load_emulator_macos_map().get(cmd)
    return None if (mapped is None or mapped == "unavailable") else mapped


def is_supported(config) -> bool:
    """Return True if this collection type is handled by the native launcher."""
    return config.id in _SUPPORTED


def build_argv(
    config,
    root: str,
    game,
    emulators: dict[str, str],
    variant: str = "",
    launch_variant=None,
) -> tuple[Optional[list[str]], str]:
    """
    Build the subprocess argv for *game*.

    *launch_variant* (a ``LaunchVariant`` from game_library) is set when the
    user chose a specific engine/config from the pre-launch dialog.  When
    provided it overrides the default conf selection or engine for eXoDOS games.

    *variant* is the platform subdirectory name for ScummVM multi-platform
    games (eXoScummVM only).  Pass an empty string to auto-detect.

    Returns ``(argv, error_message)``.  When launch is impossible *argv* is
    ``None`` and *error_message* explains why; on success *error_message* is
    the empty string.
    """
    cid = config.id
    if cid in ("exodos", "exowin3x"):
        return _dosbox_argv(config, root, game, emulators, launch_variant)
    if cid == "exoscummvm":
        return _scummvm_argv(config, root, game, emulators, variant)
    if cid == "exoappleiigs":
        return _gsplus_argv(config, root, game, emulators)
    return None, f"Native launch not supported for '{config.display_name}'."


def get_scummvm_variants(config, root: str, game) -> list[str]:
    """
    Return the available platform variant subdirectory names for a ScummVM game.

    Returns ``[]`` for non-ScummVM collections, single-path games (double-nested
    or flat), or when the game data directory is missing.  When multiple variants
    exist the list is ordered Windows-first, then alphabetically — matching the
    default Windows preference of the eXo ScummVM collection.
    """
    if config.id != "exoscummvm":
        return []
    gamename = game.game_dir
    if not gamename:
        return []
    game_data_dir = os.path.join(config.abs_game_data(root), gamename)
    if not os.path.isdir(game_data_dir):
        return []
    # Double-nested layout → only one real path, nothing to choose.
    inner = os.path.join(game_data_dir, gamename)
    if os.path.isdir(inner):
        return []
    subdirs = _list_subdirs(game_data_dir)
    if len(subdirs) <= 1:
        return []
    windows = [d for d in subdirs if "windows" in d.lower()]
    others  = [d for d in subdirs if "windows" not in d.lower()]
    return windows + others


def get_cwd(_config, root: str) -> str:
    """
    Return the working directory to use when launching a game.

    DOSBox conf files use paths relative to the inner eXo/ directory
    (e.g. ``mount c ./eXoDOS/<gamedir>``), so we must run dosbox-staging
    from that directory, not from the collection root itself.
    ScummVM and GSplus are invoked with absolute paths so CWD is arbitrary;
    using eXo/ is consistent and safe.
    """
    eXo_dir = os.path.join(root, "eXo")
    return eXo_dir if os.path.isdir(eXo_dir) else root


# ─── DOSBox (eXoDOS, eXoWin3x) ───────────────────────────────────────────────

def _dosbox_argv(
    config, root: str, game, emulators: dict[str, str], launch_variant=None
) -> tuple[Optional[list[str]], str]:
    if not game.game_dir:
        return None, f"Cannot determine game folder for '{game.title}'."
    gamedir_path = os.path.join(config.abs_scripts(root), game.game_dir)
    if not os.path.isdir(gamedir_path):
        return None, (
            f"Game scripts folder not found:\n{gamedir_path}\n\n"
            "The game may not be installed, or the game_dir is incorrect."
        )

    # When the variant specifies a ScummVM engine (eXoDOS exception.bsh), route
    # to the ScummVM launcher instead of DOSBox.
    if launch_variant is not None and getattr(launch_variant, "engine", "") == "scummvm":
        return _scummvm_argv_from_variant(config, root, game, emulators, launch_variant)

    # Conf file: use variant override when present, else auto-detect.
    if launch_variant is not None and getattr(launch_variant, "conf", ""):
        conf_name = launch_variant.conf
        game_conf = os.path.join(gamedir_path, conf_name)
        if not os.path.isfile(game_conf):
            # Fall back to the standard conf if the specific one is missing.
            game_conf = _find_dosbox_conf(gamedir_path)
    else:
        game_conf = _find_dosbox_conf(gamedir_path)

    if not game_conf:
        return None, (
            f"No DOSBox conf found in:\n{gamedir_path}\n\n"
            "Expected dosbox_linux.conf or dosbox.conf."
        )

    dosbox_cmd = _resolve_dosbox_cmd(root, game, emulators, config)
    argv = dosbox_cmd.split()
    argv += ["-conf", game_conf]

    # Global platform options conf — inside eXo/emulators/dosbox/
    if sys.platform == "darwin":
        global_conf = os.path.join(root, "eXo", "emulators", "dosbox", "options_macos.conf")
    else:
        global_conf = os.path.join(root, "eXo", "emulators", "dosbox", "options_linux.conf")
    if os.path.isfile(global_conf):
        argv += ["-conf", global_conf]

    argv += ["-nomenu", "-noconsole"]
    return argv, ""


def _scummvm_argv_from_variant(
    config, root: str, game, emulators: dict[str, str], launch_variant
) -> tuple[Optional[list[str]], str]:
    """
    Build ScummVM argv using a LaunchVariant parsed from exception.bsh.

    Used when a DOSBox-collection game (eXoDOS) has a ScummVM play option
    listed in its exception.bsh.  The variant's scummvm_path is relative
    to the collection's eXo/ directory.
    """
    scummvm_id   = getattr(launch_variant, "scummvm_id", "")
    scummvm_path = getattr(launch_variant, "scummvm_path", "")
    extra_args   = list(getattr(launch_variant, "extra_args", []))

    if not scummvm_id:
        return None, f"No ScummVM game id in launch variant for '{game.title}'."

    if scummvm_path:
        # Path from bsh is relative to the eXo/ subdirectory.
        abs_path = os.path.join(root, "eXo", *scummvm_path.replace("\\", "/").split("/"))
    else:
        abs_path = ""

    cmd = emulators.get("scummvm") or shutil.which("scummvm") or "scummvm"
    scummvm_ini = os.path.join(root, "eXo", "util", "scummvm.ini")
    argv = [cmd]
    if os.path.isfile(scummvm_ini):
        argv.append(f"--config={scummvm_ini}")
    argv += extra_args
    if abs_path:
        argv += ["-p", abs_path]
    argv.append(scummvm_id)
    return argv, ""


def _find_dosbox_conf(gamedir_path: str) -> Optional[str]:
    """Return the best DOSBox config file found in *gamedir_path*."""
    for name in ("dosbox2_linux.conf", "dosbox_linux.conf", "dosbox.conf"):
        p = os.path.join(gamedir_path, name)
        if os.path.isfile(p):
            return p
    return None


def _resolve_dosbox_cmd(root: str, game, emulators: dict[str, str], config) -> str:
    """
    Resolve the DOSBox binary: per-game override → emulators dict → default.

    Resolution order on macOS:
      1. dosbox_macos.txt  — explicit per-game macOS override (bare command)
      2. dosbox_linux.txt  — Linux flatpak command translated via emulator_macos_map.txt
      3. config.default_emulator

    On Linux only dosbox_linux.txt is consulted (step 2), used directly.
    """
    gamename = getattr(game, "gamename", "") or ""

    if gamename:
        if sys.platform == "darwin":
            macos_file = os.path.join(root, "eXo", "util", "dosbox_macos.txt")
            override = _read_dosbox_override(macos_file, gamename)
            if override:
                if override in emulators and emulators[override]:
                    return emulators[override]
                first_token = override.split()[0]
                if os.path.isabs(first_token) and os.path.isfile(first_token):
                    return override
                if shutil.which(first_token):
                    return override

            # No macOS override: translate the Linux command via the map.
            linux_file = os.path.join(root, "eXo", "util", "dosbox_linux.txt")
            linux_cmd = _read_dosbox_override(linux_file, gamename)
            if linux_cmd:
                macos_name = _map_to_macos(linux_cmd)
                if macos_name:
                    return emulators.get(macos_name) or macos_name
        else:
            linux_file = os.path.join(root, "eXo", "util", "dosbox_linux.txt")
            override = _read_dosbox_override(linux_file, gamename)
            if override:
                if override in emulators and emulators[override]:
                    return emulators[override]
                first_token = override.split()[0]
                if os.path.isabs(first_token) and os.path.isfile(first_token):
                    return override
                if shutil.which(first_token):
                    return override

    default = config.default_emulator
    return emulators.get(default) or default


def _read_dosbox_override(path: str, gamename: str) -> Optional[str]:
    """
    Return the override command for *gamename* from a dosbox override file.

    File format::
        # comment
        <GameName>:<command>
        <GameName>:<family>:<command>   (three-field variant; last field is used)
    """
    if not os.path.isfile(path):
        return None
    for raw in _iter_text_lines(path):
        line = raw.strip().rstrip("\r")
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 2)
        if parts[0].lower() == gamename.lower():
            return parts[-1]  # last field is always the command
    return None


# ─── ScummVM (eXoScummVM) ─────────────────────────────────────────────────────

def _resolve_scummvm_data_dir(game_data_dir: str, variant: str = "") -> str:
    """
    Navigate into the actual ScummVM game data directory.

    Three ZIP layouts exist in eXo ScummVM:

    1. Double-nested  — ``<gamename>/<gamename>/files``
       Single data path; most titles.
    2. Platform-split — ``<gamename>/<Platform Variant>/files``
       Multi-platform titles (e.g. Windows + Macintosh).  *variant* selects
       which platform subdir to use; falls back to Windows when empty.
    3. Flat           — ``<gamename>/files``
       Data lives directly in the outer directory.
    """
    if variant:
        candidate = os.path.join(game_data_dir, variant)
        if os.path.isdir(candidate):
            return candidate

    # Same-name inner dir (double-nested).
    gamename = os.path.basename(game_data_dir)
    inner = os.path.join(game_data_dir, gamename)
    if os.path.isdir(inner):
        return inner

    subdirs = _list_subdirs(game_data_dir)
    if not subdirs and not os.path.isdir(game_data_dir):
        return game_data_dir

    if not subdirs:
        return game_data_dir      # flat layout
    if len(subdirs) == 1:
        return os.path.join(game_data_dir, subdirs[0])

    # Multiple variants without an explicit choice: prefer Windows.
    for kw in ["windows", "dos", "linux", "macintosh", "mac"]:
        for d in subdirs:
            if kw in d.lower():
                return os.path.join(game_data_dir, d)
    return os.path.join(game_data_dir, subdirs[0])


def _scummvm_argv(
    config, root: str, game, emulators: dict[str, str], variant: str = ""
) -> tuple[Optional[list[str]], str]:
    gamename = game.game_dir
    if not gamename:
        return None, f"Cannot determine game name for '{game.title}'."

    game_data_dir = os.path.join(config.abs_game_data(root), gamename)
    if not os.path.isdir(game_data_dir):
        return None, (
            f"Game data not found:\n{game_data_dir}\n\n"
            "Install the game first via Download & Install."
        )
    game_data_dir = _resolve_scummvm_data_dir(game_data_dir, variant)

    scummvm_txt = os.path.join(root, "eXo", "util", "scummvm.txt")
    gameid = _parse_scummvm_txt(scummvm_txt, gamename)
    if not gameid:
        return None, (
            f"'{gamename}' not found in scummvm.txt.\n\n"
            "The Lite files may not be installed; try re-downloading the Lite set."
        )

    scummvm_ini = os.path.join(root, "eXo", "util", "scummvm.ini")
    cmd = emulators.get("scummvm") or shutil.which("scummvm") or "scummvm"
    argv = [cmd]
    if os.path.isfile(scummvm_ini):
        argv.append(f"--config={scummvm_ini}")
    argv += ["-p", game_data_dir, gameid]
    return argv, ""


def _parse_scummvm_txt(path: str, gamename: str) -> Optional[str]:
    """
    Return the ``engine:gameid`` token for *gamename*, or None.

    scummvm.txt format::
        <GameName>;<engine:gameid>;<version\\scummvm.exe>
    """
    if not os.path.isfile(path):
        return None
    for raw in _iter_text_lines(path):
        line = raw.strip().rstrip("\r")
        if not line:
            continue
        parts = line.split(";")
        if len(parts) >= 2 and parts[0].lower() == gamename.lower():
            return parts[1]
    return None


# ─── GSplus (eXoAppleIIGS) ────────────────────────────────────────────────────

def _gsplus_argv(
    config, root: str, game, emulators: dict[str, str]
) -> tuple[Optional[list[str]], str]:
    gamename = game.game_dir
    if not gamename:
        return None, f"Cannot determine game name for '{game.title}'."

    iigs_txt = os.path.join(root, "eXo", "util", "appleIIGS.txt")
    emu_entry = _parse_iigs_txt(iigs_txt, gamename)

    # Translate the Windows emulator token (e.g. "GSPlus\gsplus.exe", "MAME")
    # to the macOS emulator name via emulator_macos_map.txt.
    macos_emu = "gsplus"  # default when no entry or unknown token
    if emu_entry:
        mapped = _map_to_macos(emu_entry)
        if mapped:
            macos_emu = mapped
        elif emu_entry.upper() == "MAME":
            macos_emu = "mame"  # not in map but literal fallback

    if macos_emu == "mame":
        return None, (
            f"'{gamename}' requires MAME for Apple IIgs emulation.\n\n"
            "MAME support is not yet implemented in the native launcher."
        )

    config_src = os.path.join(config.abs_scripts(root), gamename, "config.txt")
    if not os.path.isfile(config_src):
        return None, (
            f"config.txt not found:\n{config_src}\n\n"
            "The Lite files may not be installed; try re-downloading the Lite set."
        )

    game_data_dir = os.path.join(config.abs_game_data(root), gamename)
    if not os.path.isdir(game_data_dir):
        return None, (
            f"Game data not found:\n{game_data_dir}\n\n"
            "Install the game first via Download & Install."
        )

    rom_path = os.path.join(root, "eXo", "emulators", "GSPlus", "ROM1")

    tmp_config = _write_gsplus_config(config_src, game_data_dir, rom_path)
    if not tmp_config:
        return None, "Failed to write temporary GSplus config file."

    cmd = emulators.get(macos_emu) or shutil.which(macos_emu) or macos_emu
    return [cmd, "-config", tmp_config], ""


def _parse_iigs_txt(path: str, gamename: str) -> Optional[str]:
    """
    Return the emulator token for *gamename* from appleIIGS.txt, or None.

    Format::
        <GameName>:<emulator_path>    e.g. ``GSPlus\\gsplus.exe`` or ``MAME``
    """
    if not os.path.isfile(path):
        return None
    for raw in _iter_text_lines(path):
        line = raw.strip().rstrip("\r")
        if not line:
            continue
        idx = line.find(":")
        if idx >= 0 and line[:idx].lower() == gamename.lower():
            return line[idx + 1:]
    return None


def _write_gsplus_config(
    config_src: str, game_data_dir: str, rom_path: str
) -> Optional[str]:
    """
    Read the Windows-style GSplus config.txt and write a temp copy with
    Unix absolute paths for ROM and disk images.  Returns the temp file path,
    or None on error.
    """
    try:
        with open(config_src, "r", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    out: list[str] = []
    for raw in lines:
        stripped = raw.rstrip("\r\n")

        # Replace ROM path
        if re.match(r"\s*g_cfg_rom_path\s*=", stripped):
            out.append(f"g_cfg_rom_path = {rom_path}\n")
            continue

        # Replace disk-image paths (s5d1, s6d1, s7d1, s7d2, …)
        m = re.match(r"^(\s*s\d+d\d+\s*=\s*)(.*)$", stripped)
        if m:
            prefix, val = m.group(1), m.group(2).strip()
            if val and not os.path.isabs(val):
                val = os.path.join(game_data_dir, val.replace("\\", os.sep))
            out.append(f"{prefix}{val}\n")
            continue

        out.append(raw if raw.endswith("\n") else stripped + "\n")

    try:
        fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="exo_gsplus_")
        with os.fdopen(fd, "w") as fh:
            fh.writelines(out)
        return tmp
    except OSError:
        return None
