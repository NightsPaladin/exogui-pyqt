"""
game_library.py — Parse a LaunchBox XML catalogue and build the game list.

Supports multiple eXo collection types via ProjectConfig (see core/project.py).
Resolves:
 - Game metadata from the collection's XML file
 - Emulator per game from eXo/util/dosbox_macos.txt or dosbox_linux.txt
 - Box-art / screenshot paths from the collection's Images/ directory
 - Installed status (game data directory exists in game_data_subdir)
"""

from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from core.project import ProjectConfig, EXODOS
from core import aria_index as _aria_index


# ── helpers ──────────────────────────────────────────────────────────────────

def _text(element, tag: str, default: str = "") -> str:
    node = element.find(tag)
    if node is not None and node.text:
        return node.text.strip()
    return default


def _title_to_image_stem(title: str) -> str:
    """
    Convert a game title to the filename stem used in the Images/ directory.
    LaunchBox replaces characters that are illegal on Windows with underscores:
      : / \\ * ? " < > |  →  _
    Also drops leading/trailing spaces.
    """
    stem = re.sub(r'[:/\\*?"<>|]', "_", title)
    return stem.strip()


IMAGE_TYPES = {
    "box_front":    "Box - Front",
    "screenshot":   "Screenshot - Gameplay",
    "box_3d":       "Box - 3D",
    "banner":       "Banner",
    "background":   "Fanart - Background",
    "clear_logo":   "Clear Logo",
}

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".JPG")

_IS_LINUX = sys.platform.startswith("linux")


def emulator_display_name(raw: str) -> str:
    """
    Return a short human-readable emulator label from the raw emulator string.

    macOS entries are already short (e.g. "dosbox-staging", "dosbox-x", "scummvm").
    Linux entries are flatpak invocations like
      "flatpak run com.retro_exo.dosbox-staging-082-0"
    which we abbreviate to just the emulator family.
    """
    if not raw:
        return ""
    if "flatpak" not in raw:
        return raw   # macOS: already short

    # Extract the flatpak app-id component after the last dot
    m = re.search(r"com\.retro_exo\.([^\s]+)", raw)
    if not m:
        return raw
    app_id = m.group(1)  # e.g. "dosbox-staging-082-0"

    # Normalise to family name
    for family in ("dosbox-staging", "dosbox-x", "dosbox-ece", "dosbox-074",
                   "dosbox-gridc", "scummvm", "gzdoom", "wine", "vlc", "aria2c"):
        if app_id.startswith(family.replace("-", "-")):
            return family
    return app_id


# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class Extra:
    """A single file from the game's Extras/ folder."""
    name: str          # display name (filename without extension)
    path: str          # absolute path
    kind: str          # 'video' | 'pdf' | 'document' | 'audio' | 'image' | 'other'
    ext: str           # lowercase extension without dot


EXTRA_KINDS: dict[str, str] = {
    "mp4": "video", "avi": "video", "mkv": "video", "mov": "video",
    "mp3": "audio", "flac": "audio", "wav": "audio", "ogg": "audio",
    "pdf": "pdf",
    "txt": "document", "htm": "document", "html": "document",
    "doc": "document", "docx": "document", "rtf": "document",
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "cbr": "document", "cbz": "document",
}

# Files in Extras/ that are launcher scripts, not content
_EXTRA_SKIP_EXTS = {".bat", ".bsh", ".command", ".exe", ".sh"}


@dataclass
class Game:
    id: str
    title: str
    sort_title: str
    app_path: str           # Windows-style relative path e.g. eXo\eXoDOS\!dos\<dir>\<name>.bat
    root_folder: str        # Windows-style e.g. eXo\eXoDOS\!dos\<dir>
    platform: str
    genre: str
    developer: str
    publisher: str
    release_year: int       # 0 if unknown
    rating: str
    community_rating: float
    notes: str
    series: str
    play_mode: str
    max_players: int
    source: str
    # resolved at library load time
    game_dir: str = ""          # short folder name, e.g. "$100000P"
    gamename: str = ""          # launch-script stem, e.g. "Dune 2 - … (1992)"
    emulator: str = ""          # raw emulator command (platform-specific)
    installed: bool = False
    zip_present: bool = True    # False when ZIP is absent (Lite mode)
    download_size_str: str = "" # non-empty when zip_present=False and game is in torrent index
    image_paths: dict = field(default_factory=dict)   # type→abs_path
    compat_note: str = ""       # non-empty → game has limited/no macOS support
    extras: list = field(default_factory=list)        # list[Extra]

    @property
    def emulator_display(self) -> str:
        """Short human-readable emulator label suitable for the UI."""
        return emulator_display_name(self.emulator)

    @property
    def display_year(self) -> str:
        return str(self.release_year) if self.release_year else "Unknown"

    @property
    def genres(self) -> list[str]:
        return [g.strip() for g in self.genre.split(";") if g.strip()]

    @property
    def first_genre(self) -> str:
        return self.genres[0] if self.genres else ""


# ── library ──────────────────────────────────────────────────────────────────

class GameLibrary:
    """
    Loads and caches a game catalogue from an eXo collection.

    Parameters
    ----------
    root : str
        Absolute path to the collection root (the folder containing eXo/,
        xml/, Images/).
    xml_mode : str
        Which XML variant to load.  Available modes depend on the project type;
        ``"auto"`` always works and picks the best available file.
    config : ProjectConfig, optional
        Describes the collection's path layout.  Defaults to EXODOS for
        backward compatibility.
    """

    def __init__(self, root: str, xml_mode: str = "auto",
                 config: Optional[ProjectConfig] = None):
        self.root = root
        self.xml_mode = xml_mode
        self._config = config if config is not None else EXODOS

        self.games: list[Game] = []
        self._by_id: dict[str, Game] = {}
        self._emulator_map: dict[str, str] = {}   # lower(title) → emulator cmd
        self._wine_notes: dict[str, str] = {}     # lower(title) → macOS warning note

        self._image_base = self._config.abs_image_base(root)
        self._dos_base   = self._config.abs_scripts(root)
        self._zip_base   = self._config.abs_game_data(root)
        self._aria: dict[str, _aria_index.GameEntry] = {}

    @property
    def config(self) -> ProjectConfig:
        return self._config

    # ── public ────────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Parse XML and build the game list. Call once at startup."""
        self._load_emulator_map()
        self._parse_xml()
        self._load_aria_index()
        self._resolve_installation()
        self._resolve_images()
        self._resolve_extras()

    def search(self, query: str) -> list[Game]:
        """Return games whose title, genre, or developer contains *query* (case-insensitive)."""
        q = query.lower()
        return [g for g in self.games
                if q in g.title.lower()
                or q in g.genre.lower()
                or q in g.developer.lower()
                or q in g.publisher.lower()]

    def filter_by_genre(self, genre: str) -> list[Game]:
        if not genre:
            return self.games
        g = genre.lower()
        return [gm for gm in self.games if g in gm.genre.lower()]

    def filter_installed(self) -> list[Game]:
        return [g for g in self.games if g.installed]

    def all_genres(self) -> list[str]:
        genres: set[str] = set()
        for g in self.games:
            genres.update(g.genres)
        return sorted(genres)

    def all_years(self) -> list[int]:
        return sorted({g.release_year for g in self.games if g.release_year})

    def all_ratings(self) -> list[str]:
        return sorted({g.rating for g in self.games if g.rating})

    def all_play_modes(self) -> list[str]:
        """Return unique individual play-mode values (semicolon-separated in XML)."""
        modes: set[str] = set()
        for g in self.games:
            for m in g.play_mode.split(";"):
                m = m.strip()
                if m:
                    modes.add(m)
        return sorted(modes)

    def get_by_id(self, game_id: str) -> Optional[Game]:
        return self._by_id.get(game_id)

    # ── private ───────────────────────────────────────────────────────────────

    def _load_emulator_map(self) -> None:
        """
        Load the platform-appropriate dosbox_*.txt → {lower_title: emulator_command}

        macOS: eXo/util/dosbox_macos.txt  (bare commands: dosbox-staging, dosbox-x …)
        Linux: eXo/util/dosbox_linux.txt  (flatpak invocations)

        Entries use the format "Game Title (Year):emulator".
        The year suffix is stripped so we can match against XML titles.

        Also scans the *other* platform's file on macOS to flag games that require
        Wine (Windows-only DOSBox variants unavailable on macOS).
        """
        _year_re = re.compile(r"\s*\(\d{4}\)\s*$")

        primary_file = "dosbox_linux.txt" if _IS_LINUX else "dosbox_macos.txt"
        path = os.path.join(self.root, "eXo", "util", primary_file)
        if not os.path.exists(path):
            return
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if ":" not in line:
                    continue
                title_with_year, _, emulator = line.partition(":")
                title = _year_re.sub("", title_with_year).strip()
                emu = emulator.strip()
                self._emulator_map[title.lower()] = emu
                self._emulator_map[title_with_year.strip().lower()] = emu

        # On macOS: scan dosbox_linux.txt to find games that need Wine (Windows-only
        # DOSBox variants).  These are flagged with a compatibility note in the UI.
        if not _IS_LINUX:
            _emu_notes = {
                "gunstick_dosbox": (
                    "Requires GunStick DOSBox (Windows-only) — "
                    "light gun peripheral support is unavailable on macOS"
                ),
                "daum": (
                    "Requires DOSBox DAUM (Windows-only) — "
                    "3DFX Glide emulation is unavailable on macOS; game may not run correctly"
                ),
            }
            linux_path = os.path.join(self.root, "eXo", "util", "dosbox_linux.txt")
            if os.path.exists(linux_path):
                with open(linux_path, "r", errors="replace") as fh:
                    for raw in fh:
                        line = raw.strip()
                        if "wine" not in line.lower() or ":" not in line:
                            continue
                        title_with_year, _, cmd = line.partition(":")
                        title = _year_re.sub("", title_with_year).strip()
                        note = "Requires Windows-only DOSBox variant — may not run correctly on macOS"
                        for key, specific_note in _emu_notes.items():
                            if key.lower() in cmd.lower():
                                note = specific_note
                                break
                        self._wine_notes[title.lower()] = note
                        self._wine_notes[title_with_year.strip().lower()] = note

    def _parse_xml(self) -> None:
        xml_path = self._config.xml_path(self.root, self.xml_mode)
        if not os.path.exists(xml_path):
            # Fall back to "all" if the requested variant doesn't exist
            fallback = self._config.xml_path(self.root, "all")
            xml_path = fallback if os.path.exists(fallback) else xml_path

        tree = ET.parse(xml_path)
        root = tree.getroot()

        for elem in root.findall("Game"):
            title = _text(elem, "Title")
            if not title:
                continue

            release_str = _text(elem, "ReleaseDate")
            year = 0
            if release_str:
                m = re.match(r"(\d{4})", release_str)
                if m:
                    year = int(m.group(1))

            community_rating_str = _text(elem, "CommunityStarRating", "0")
            try:
                community_rating = float(community_rating_str)
            except ValueError:
                community_rating = 0.0

            max_players_str = _text(elem, "MaxPlayers", "1")
            try:
                max_players = int(max_players_str)
            except ValueError:
                max_players = 1

            sort_title = _text(elem, "SortTitle") or title

            game = Game(
                id=_text(elem, "ID"),
                title=title,
                sort_title=sort_title,
                app_path=_text(elem, "ApplicationPath"),
                root_folder=_text(elem, "RootFolder"),
                platform=_text(elem, "Platform"),
                genre=_text(elem, "Genre"),
                developer=_text(elem, "Developer"),
                publisher=_text(elem, "Publisher"),
                release_year=year,
                rating=_text(elem, "Rating"),
                community_rating=community_rating,
                notes=_text(elem, "Notes"),
                series=_text(elem, "Series"),
                play_mode=_text(elem, "PlayMode"),
                max_players=max_players,
                source=_text(elem, "Source"),
            )

            # Derive game_dir from RootFolder
            # e.g. "eXo\eXoDOS\!dos\captlsm" → "captlsm"
            root_folder_unix = game.root_folder.replace("\\", "/")
            game.game_dir = root_folder_unix.split("/")[-1] if root_folder_unix else ""

            # Emulator from map; fall back to project default
            game.emulator = self._emulator_map.get(
                title.lower(), self._config.default_emulator
            )

            # macOS compatibility note (e.g. Wine-dependent games)
            game.compat_note = self._wine_notes.get(title.lower(), "")

            self.games.append(game)
            if game.id:
                self._by_id[game.id] = game

        self.games.sort(key=lambda g: g.sort_title.lower())

    def _load_aria_index(self) -> None:
        """Load the torrent index file into a gamename → GameEntry dict."""
        index_path = self._config.aria_index_path(self.root)
        self._aria = _aria_index.load_index(index_path)

    def _resolve_installation(self) -> None:
        """
        For each game, set:
        - ``gamename``      — the launch-script stem (title + year), e.g.
                              ``"Dune 2 - The Building of a Dynasty (1992)"``.
                              Used to locate the ZIP file and torrent index entry.
        - ``installed``     — True if the extracted game data directory exists.
        - ``zip_present``   — True if the ZIP archive is on disk.  False in Lite
                              mode when only metadata has been downloaded.
        - ``download_size_str`` — human-readable size from the torrent index when
                              the ZIP is absent; empty string otherwise.
        """
        game_data_base = self._zip_base
        year_pattern = re.compile(r"\(\d{4}\)\.(bsh|msh|command|sh)$")

        for game in self.games:
            if not game.game_dir:
                continue

            # ── installed = extracted game data directory exists ───────────
            game.installed = os.path.isdir(
                os.path.join(game_data_base, game.game_dir)
            )

            # ── derive gamename from the launch script in !dos/<gamedir>/ ──
            game_folder = os.path.join(self._dos_base, game.game_dir)
            gamename = ""
            if os.path.isdir(game_folder):
                try:
                    for fname in sorted(os.listdir(game_folder)):
                        if fname.startswith("._"):
                            continue
                        if year_pattern.search(fname):
                            gamename = os.path.splitext(fname)[0]
                            break
                except OSError:
                    pass
            game.gamename = gamename

            # ── zip_present ───────────────────────────────────────────────
            if gamename:
                zip_path = os.path.join(game_data_base, gamename + ".zip")
                game.zip_present = os.path.isfile(zip_path)
                if not game.zip_present:
                    entry = self._aria.get(gamename)
                    if entry:
                        game.download_size_str = entry.total_size_str
            else:
                # Cannot determine ZIP name: assume present so we don't show a
                # misleading "Download & Install" button
                game.zip_present = True

    def _resolve_images(self) -> None:
        """Find box-art and screenshot images for each game."""
        # Build a set of all available filenames per image type for fast lookup
        type_files: dict[str, dict[str, str]] = {}   # type → {lower_stem: abs_path}
        for img_type_key, img_type_dir in IMAGE_TYPES.items():
            dirpath = os.path.join(self._image_base, img_type_dir)
            if not os.path.isdir(dirpath):
                continue
            files: dict[str, str] = {}
            for fname in os.listdir(dirpath):
                stem, ext = os.path.splitext(fname)
                if ext.lower().lstrip(".") in ("png", "jpg", "jpeg", "gif"):
                    files[stem.lower()] = os.path.join(dirpath, fname)
            type_files[img_type_key] = files

        for game in self.games:
            stem = _title_to_image_stem(game.title)
            stem_lower = stem.lower()

            for img_type_key in IMAGE_TYPES:
                files = type_files.get(img_type_key, {})
                if not files:
                    continue
                # Try exact match with index suffixes -00, -01, -02
                for idx in ("00", "01", "02", "03"):
                    candidate = f"{stem_lower}-{idx}"
                    if candidate in files:
                        game.image_paths[img_type_key] = files[candidate]
                        break
                # Screenshots: gather all
                if img_type_key == "screenshot":
                    shots = []
                    for idx in ("01", "02", "03", "04", "05", "06", "07", "08"):
                        candidate = f"{stem_lower}-{idx}"
                        if candidate in files:
                            shots.append(files[candidate])
                    game.image_paths["screenshots"] = shots  # type: ignore[assignment]

    def _resolve_extras(self) -> None:
        """Load videos, documents, and other extras from each game's Extras/ folder."""
        for game in self.games:
            if not game.game_dir:
                continue
            extras_dir = os.path.join(self._dos_base, game.game_dir, "Extras")
            if not os.path.isdir(extras_dir):
                continue
            items: list[Extra] = []
            try:
                for fname in sorted(os.listdir(extras_dir)):
                    if fname.startswith("._") or fname.startswith("."):
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in _EXTRA_SKIP_EXTS:
                        continue
                    kind = EXTRA_KINDS.get(ext.lstrip("."), "other")
                    items.append(Extra(
                        name=os.path.splitext(fname)[0],
                        path=os.path.join(extras_dir, fname),
                        kind=kind,
                        ext=ext.lstrip("."),
                    ))
            except OSError:
                pass
            game.extras = items
