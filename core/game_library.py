"""
game_library.py — Parse a LaunchBox XML catalogue and build the game list.

Supports multiple eXo collection types via ProjectConfig (see core/project.py).
Resolves:
  - Game metadata from the collection's XML file
  - Emulator per game from eXo/util/dosbox_macos.txt or dosbox_linux.txt
  - Sanitization-aware image, music, and video matches from the collection media directories
  - Installed status (game data directory exists in game_data_subdir)
"""

from __future__ import annotations

import hashlib
import os
import pickle
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

# ── pre-compiled patterns (avoid per-call re-cache lookups) ──────────────────

_RE_LEADING_ELLIPSIS_ARTICLE = re.compile(
    r"^(?:\.{3}|…)\s*(?:The|A|An)\s+", re.IGNORECASE
)
_RE_AMP = re.compile(r"\s*&\s*")
_RE_WORD_NUMS = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\b", re.IGNORECASE
)
_WORD_TO_NUM: dict[str, str] = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}
# Roman numerals: longest alternatives first to avoid partial matches
_RE_ROMAN_NUMS = re.compile(
    r"\b(viii|vii|iii|ix|vi|iv|ii|x|v|i)\b", re.IGNORECASE
)
_ROMAN_TO_NUM: dict[str, str] = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
    "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10",
}
_RE_COLON       = re.compile(r"\s*:\s*")
_RE_DASH        = re.compile(r"\s+-\s+")
_RE_MULTI_SPACE = re.compile(r"\s+")
_RE_NON_ALNUM   = re.compile(r"[^0-9a-z]+")
_RE_TRAILING_ARTICLE = re.compile(r"^(?P<body>.+), (?P<article>The|A|An)$", re.IGNORECASE)
_RE_LEADING_ARTICLE  = re.compile(r"^(?P<article>The|A|An) (?P<body>.+)$",  re.IGNORECASE)
_RE_YEAR_SUFFIX = re.compile(r"\s*\(\d{4}\)\s*$")
_RE_RELEASE_YEAR = re.compile(r"(\d{4})")
_RE_INSTALL_YEAR_SCRIPT = re.compile(r"\(\d{4}\)\.(bsh|msh|command|sh)$")

from core.project import ProjectConfig, EXODOS
from core import aria_index as _aria_index


# ── helpers ──────────────────────────────────────────────────────────────────

def _text(element, tag: str, default: str = "") -> str:
    node = element.find(tag)
    if node is not None and node.text:
        return node.text.strip()
    return default


_NAME_YEAR_RE = re.compile(r"^(?P<title>.+?)(?: )?\((?P<year>\d{4}|[12]\d{2}x)\)$")


def _launch_stem_from_path(rel_path: str) -> str:
    cleaned = rel_path.replace("\\", "/").strip()
    if not cleaned:
        return ""
    return os.path.splitext(os.path.basename(cleaned))[0].strip()


@lru_cache(maxsize=65536)
def _strip_name_year(value: str) -> str:
    match = _NAME_YEAR_RE.fullmatch(value.strip())
    if match is not None:
        return match.group("title").strip()
    return value.strip()


def _unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return ordered


def _normalize_article_segment(segment: str) -> str:
    """Canonicalize a single title segment's leading/trailing article."""
    segment = segment.strip()
    m = _RE_TRAILING_ARTICLE.fullmatch(segment)
    if m:
        return f"{m.group('body')}, {m.group('article').title()}"
    m = _RE_LEADING_ARTICLE.fullmatch(segment)
    if m:
        return f"{m.group('body')}, {m.group('article').title()}"
    return segment


@lru_cache(maxsize=262144)
def _normalize_media_common(value: str) -> str:
    """
    Shared normalization steps (steps 1-6) for both key and skeleton functions.
    Handles ellipsis-articles, punctuation cleanup, word/roman numeral → digit.
    """
    normalized = _RE_LEADING_ELLIPSIS_ARTICLE.sub("", value.strip())
    normalized = normalized.replace("...", "").replace("…", "").replace("_", " ")
    normalized = _RE_AMP.sub(" and ", normalized)
    normalized = _RE_WORD_NUMS.sub(lambda m: _WORD_TO_NUM[m.group(0).lower()], normalized)
    normalized = _RE_ROMAN_NUMS.sub(lambda m: _ROMAN_TO_NUM[m.group(0).lower()], normalized)
    return normalized


@lru_cache(maxsize=262144)
def _normalize_media_match_key(value: str) -> str:
    normalized = _normalize_media_common(value)
    normalized = _RE_COLON.sub(" - ", normalized)
    normalized = _RE_DASH.sub(" - ", normalized)
    normalized = _RE_MULTI_SPACE.sub(" ", normalized)
    normalized = " - ".join(_normalize_article_segment(p) for p in normalized.split(" - "))
    return normalized.casefold()


@lru_cache(maxsize=262144)
def _normalize_media_match_skeleton(value: str) -> str:
    normalized = _normalize_media_common(value)
    normalized = _RE_COLON.sub(" ", normalized)
    normalized = _RE_DASH.sub(" ", normalized)
    normalized = _RE_MULTI_SPACE.sub(" ", normalized)
    return _RE_NON_ALNUM.sub("", normalized.casefold())


IMAGE_TYPES = {
    "box_front": "Box - Front",
    "box_front_reconstructed": "Box - Front - Reconstructed",
    "fanart_box_front": "Fanart - Box - Front",
    "box_3d": "Box - 3D",
    "box_full": "Box - Full",
    "box_back": "Box - Back",
    "box_back_reconstructed": "Box - Back - Reconstructed",
    "box_spine": "Box - Spine",
    "cart_front": "Cart - Front",
    "cart_back": "Cart - Back",
    "cart_3d": "Cart - 3D",
    "disc": "Disc",
    "advertisement_flyer_front": "Advertisement Flyer - Front",
    "advertisement_flyer_back": "Advertisement Flyer - Back",
    "banner": "Banner",
    "clear_logo": "Clear Logo",
    "background": "Fanart - Background",
    "fanart_disc": "Fanart - Disc",
    "fanart_box_back": "Fanart - Box - Back",
    "fanart_cart_front": "Fanart - Cart - Front",
    "fanart_cart_back": "Fanart - Cart - Back",
    "screenshot": "Screenshot - Gameplay",
    "screenshot_game_title": "Screenshot - Game Title",
    "screenshot_game_select": "Screenshot - Game Select",
    "screenshot_high_scores": "Screenshot - High Scores",
    "screenshot_game_over": "Screenshot - Game Over",
    "amazon_background": "Amazon Background",
    "amazon_poster": "Amazon Poster",
    "amazon_screenshot": "Amazon Screenshot",
    "epic_games_background": "Epic Games Background",
    "epic_games_poster": "Epic Games Poster",
    "epic_games_screenshot": "Epic Games Screenshot",
    "gog_poster": "GOG Poster",
    "gog_screenshot": "GOG Screenshot",
    "origin_background": "Origin Background",
    "origin_poster": "Origin Poster",
    "origin_screenshot": "Origin Screenshot",
    "steam_banner": "Steam Banner",
    "steam_poster": "Steam Poster",
    "steam_screenshot": "Steam Screenshot",
    "uplay_background": "Uplay Background",
    "uplay_thumbnail": "Uplay Thumbnail",
    "arcade_cabinet": "Arcade - Cabinet",
    "arcade_control_panel": "Arcade - Control Panel",
    "arcade_controls_information": "Arcade - Controls Information",
    "arcade_marquee": "Arcade - Marquee",
    "arcade_circuit_board": "Arcade - Circuit Board",
}

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif")

_IMAGE_INDEX_LIMIT = 20
_SINGLE_IMAGE_INDEXES = tuple(f"{idx:02d}" for idx in range(_IMAGE_INDEX_LIMIT + 1))
_SCREENSHOT_INDEXES = tuple(f"{idx:02d}" for idx in range(_IMAGE_INDEX_LIMIT + 1))
_COVER_IMAGE_TYPES = (
    "box_front",
    "box_front_reconstructed",
    "fanart_box_front",
    "box_3d",
    "box_full",
    "cart_front",
    "cart_3d",
    "disc",
    "advertisement_flyer_front",
    "steam_poster",
    "gog_poster",
    "epic_games_poster",
    "origin_poster",
    "amazon_poster",
    "uplay_thumbnail",
    "box_back",
    "box_back_reconstructed",
    "cart_back",
    "box_spine",
    "banner",
    "clear_logo",
    "fanart_disc",
    "fanart_box_back",
    "fanart_cart_front",
    "fanart_cart_back",
    "steam_banner",
    "amazon_background",
    "origin_background",
    "epic_games_background",
    "uplay_background",
    "background",
    "arcade_cabinet",
    "arcade_marquee",
    "arcade_control_panel",
    "arcade_controls_information",
    "arcade_circuit_board",
)
_SCREENSHOT_IMAGE_TYPES = (
    "screenshot",
    "screenshot_game_title",
    "screenshot_game_select",
    "screenshot_high_scores",
    "screenshot_game_over",
    "steam_screenshot",
    "gog_screenshot",
    "epic_games_screenshot",
    "origin_screenshot",
    "amazon_screenshot",
)
_DETAIL_GALLERY_IMAGE_TYPES = tuple(dict.fromkeys(
    _SCREENSHOT_IMAGE_TYPES
    + _COVER_IMAGE_TYPES
    + tuple(
        key for key in IMAGE_TYPES
        if key not in _SCREENSHOT_IMAGE_TYPES and key not in _COVER_IMAGE_TYPES
    )
))

_IS_LINUX = sys.platform.startswith("linux")

# ── disk cache ────────────────────────────────────────────────────────────────
# Bump _CACHE_VERSION whenever the Game dataclass or resolution logic changes
# in a way that makes old cache files incompatible.
_CACHE_VERSION = 1
_CACHE_BASE    = os.path.expanduser("~/.cache/exogui/library")


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
_COLLECTION_MUSIC_EXTENSIONS = (
    ".amf", ".dsm", ".m3u", ".mo3", ".mod", ".mp2", ".mp3",
    ".ogg", ".psm", ".s3m", ".sfx", ".voc", ".wav", ".xm",
)
_COLLECTION_VIDEO_EXTENSIONS = (".mp4", ".avi", ".mkv", ".mov")


@dataclass
class StemLookupIndex:
    exact: dict[str, list[str]] = field(default_factory=dict)
    normalized: dict[str, list[str]] = field(default_factory=dict)
    skeleton: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateStemGroup:
    exact: tuple[str, ...]
    normalized: tuple[str, ...]
    skeleton: tuple[str, ...]


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
    launch_stem: str = ""       # XML ApplicationPath basename without extension
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

    @property
    def primary_cover_path(self) -> str:
        cover = self.image_paths.get("cover")
        if isinstance(cover, str):
            return cover
        box_front = self.image_paths.get("box_front")
        if isinstance(box_front, str):
            return box_front
        shots = self.image_paths.get("screenshots")
        if isinstance(shots, list) and shots:
            first = shots[0]
            if isinstance(first, str):
                return first
        return ""


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

    def load(self, force_reload: bool = False) -> None:
        """
        Parse XML and build the game list. Call once at startup.

        On repeated launches with unchanged collection files the resolved data is
        served from a pickle cache in ~/.cache/exogui/library/, skipping the XML
        parse, image directory walk, extras scan, and media resolution entirely.
        Only installation state (installed / zip_present) is always re-checked via
        the fast os.scandir path.

        Pass force_reload=True (e.g. from the Refresh action) to bypass the cache.
        """
        if not force_reload and self._try_load_cache():
            self._load_aria_index()
            self._resolve_installation()
            return

        # Full load path ────────────────────────────────────────────────────────
        self._load_emulator_map()
        self._load_aria_index()

        # Overlap XML parse (needs emulator map) with image directory walk (pure I/O)
        with ThreadPoolExecutor(max_workers=2) as pool:
            xml_f   = pool.submit(self._parse_xml)
            image_f = pool.submit(self._index_image_files)
            xml_f.result()                   # re-raises on parse error
            type_files = image_f.result()

        self._resolve_installation()
        self._resolve_images(type_files)
        self._resolve_extras()
        self._resolve_collection_media()
        self._save_cache()

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

    # ── disk cache ────────────────────────────────────────────────────────────

    def _cache_key(self) -> str:
        """
        MD5 fingerprint of the files that affect the fully-resolved game list.
        Any mtime change invalidates the cache for this project.
        """
        emu_file = "dosbox_linux.txt" if _IS_LINUX else "dosbox_macos.txt"
        paths = [
            self._config.xml_path(self.root, self.xml_mode),
            self._image_base,
            self._dos_base,
            self._config.abs_music_base(self.root),
            self._config.abs_video_base(self.root),
            os.path.join(self.root, "eXo", "util", emu_file),
        ]
        parts = [self._config.id, self.xml_mode, str(_CACHE_VERSION)]
        for p in paths:
            try:
                parts.append(f"{os.path.getmtime(p):.0f}")
            except OSError:
                parts.append("0")
        return hashlib.md5("|".join(parts).encode()).hexdigest()

    def _cache_path(self) -> str:
        return os.path.join(_CACHE_BASE, f"{self._config.id}_{self._cache_key()}.pkl")

    def _try_load_cache(self) -> bool:
        """
        Attempt to load pre-resolved game data from disk.
        Returns True and populates self.games / self._by_id on success.
        Silent on any error (corrupt file, version mismatch, etc.).
        """
        try:
            path = self._cache_path()
            if not os.path.exists(path):
                return False
            with open(path, "rb") as fh:
                data = pickle.load(fh)
            if data.get("version") != _CACHE_VERSION:
                return False
            self.games  = data["games"]
            self._by_id = data["by_id"]
            return True
        except Exception:
            return False

    def _save_cache(self) -> None:
        """
        Persist fully-resolved game data to disk for use on the next launch.
        Removes any stale cache files for this project config first.
        Silent on any error (disk full, permissions, etc.).
        """
        try:
            os.makedirs(_CACHE_BASE, exist_ok=True)
            path   = self._cache_path()
            prefix = f"{self._config.id}_"
            # Evict stale cache files for this project
            for fname in os.listdir(_CACHE_BASE):
                if fname.startswith(prefix) and fname != os.path.basename(path):
                    try:
                        os.remove(os.path.join(_CACHE_BASE, fname))
                    except OSError:
                        pass
            with open(path, "wb") as fh:
                pickle.dump(
                    {"version": _CACHE_VERSION, "games": self.games, "by_id": self._by_id},
                    fh,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
        except Exception:
            pass

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
                title = _RE_YEAR_SUFFIX.sub("", title_with_year).strip()
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
                        title = _RE_YEAR_SUFFIX.sub("", title_with_year).strip()
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
                m = _RE_RELEASE_YEAR.match(release_str)
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
                launch_stem=_launch_stem_from_path(_text(elem, "ApplicationPath")),
            )

            # Derive game_dir from RootFolder
            # e.g. "eXo\eXoDOS\!dos\captlsm" → "captlsm"
            root_folder_unix = game.root_folder.replace("\\", "/")
            game.game_dir = root_folder_unix.split("/")[-1] if root_folder_unix else ""

            # Skip utility/admin entries that point outside the project tree
            # (e.g. the "Setup eXoDOS" shortcut entry whose RootFolder is "..").
            # All real games have root_folder starting with "eXo/".
            if root_folder_unix.startswith(".."):
                continue

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

        Uses a single os.scandir pass over game_data_base to build installed_dirs and
        zip_stems sets, replacing per-game os.path.isdir / os.path.isfile calls.
        Similarly pre-scans _dos_base to build a gamename_map.
        """
        game_data_base = self._zip_base

        # ── Pre-scan game_data_base once: collect installed dirs and ZIP stems ──
        installed_dirs: set[str] = set()
        zip_stems: set[str] = set()
        try:
            with os.scandir(game_data_base) as it:
                for entry in it:
                    if entry.is_dir():
                        installed_dirs.add(entry.name)
                    elif entry.is_file() and entry.name.lower().endswith(".zip"):
                        zip_stems.add(os.path.splitext(entry.name)[0])
        except OSError:
            pass

        # ── Pre-scan _dos_base once: build gamedir → gamename map ─────────────
        gamename_map: dict[str, str] = {}
        try:
            with os.scandir(self._dos_base) as outer:
                for dir_entry in outer:
                    if not dir_entry.is_dir():
                        continue
                    try:
                        for fname in sorted(os.listdir(dir_entry.path)):
                            if fname.startswith("._"):
                                continue
                            if _RE_INSTALL_YEAR_SCRIPT.search(fname):
                                gamename_map[dir_entry.name] = os.path.splitext(fname)[0]
                                break
                    except OSError:
                        pass
        except OSError:
            pass

        for game in self.games:
            if not game.game_dir:
                continue

            game.installed = game.game_dir in installed_dirs

            gamename = gamename_map.get(game.game_dir) or game.launch_stem
            game.gamename = gamename

            if gamename:
                game.zip_present = gamename in zip_stems
                if not game.zip_present:
                    entry = self._aria.get(gamename)
                    if entry:
                        game.download_size_str = entry.total_size_str
            else:
                # Cannot determine ZIP name: assume present so we don't show a
                # misleading "Download & Install" button
                game.zip_present = True

    def _resolve_images(self, type_files: dict | None = None) -> None:
        """Find box-art and screenshot images for each game.

        *type_files* may be a pre-built index from _index_image_files() (used
        when it was computed in parallel with XML parsing).
        """
        if type_files is None:
            type_files = self._index_image_files()

        for game in self.games:
            image_bases = self._image_candidate_bases(game)
            single_candidates = self._candidate_stem_groups(image_bases, _SINGLE_IMAGE_INDEXES)
            screenshot_candidates = self._candidate_stem_groups(image_bases, _SCREENSHOT_INDEXES)

            for img_type_key in IMAGE_TYPES:
                match = self._first_image_match(type_files.get(img_type_key, StemLookupIndex()), single_candidates)
                if match:
                    game.image_paths[img_type_key] = match

            shots: list[str] = []
            seen_shots: set[str] = set()
            for img_type_key in _SCREENSHOT_IMAGE_TYPES:
                for shot in self._all_image_matches(type_files.get(img_type_key, StemLookupIndex()), screenshot_candidates):
                    if shot not in seen_shots:
                        shots.append(shot)
                        seen_shots.add(shot)
            game.image_paths["screenshots"] = shots  # type: ignore[assignment]

            gallery: list[str] = []
            seen_gallery: set[str] = set()
            for img_type_key in _DETAIL_GALLERY_IMAGE_TYPES:
                for image in self._all_image_matches(type_files.get(img_type_key, StemLookupIndex()), single_candidates):
                    if image not in seen_gallery:
                        gallery.append(image)
                        seen_gallery.add(image)
            game.image_paths["gallery"] = gallery  # type: ignore[assignment]

            for img_type_key in _COVER_IMAGE_TYPES:
                match = self._first_image_match(type_files.get(img_type_key, StemLookupIndex()), single_candidates)
                if match:
                    game.image_paths["cover"] = match
                    break

    def _index_image_files(self) -> dict[str, StemLookupIndex]:
        """Recursively index supported image files by LaunchBox image category."""
        collected: dict[str, list[tuple[str, str]]] = {}
        if not os.path.isdir(self._image_base):
            return {}

        for dirpath, _, filenames in os.walk(self._image_base):
            rel_dir = os.path.relpath(dirpath, self._image_base)
            if rel_dir == ".":
                continue
            top_level = rel_dir.split(os.sep, 1)[0]
            img_type_key = next(
                (key for key, folder in IMAGE_TYPES.items() if folder == top_level),
                None,
            )
            if not img_type_key:
                continue

            files = collected.setdefault(img_type_key, [])
            for fname in filenames:
                if fname.startswith("._"):
                    continue
                stem, ext = os.path.splitext(fname)
                if ext.lower() not in IMAGE_EXTENSIONS:
                    continue
                files.append((stem, os.path.join(dirpath, fname)))

        image_base = self._image_base

        def _image_sort_key(path: str) -> tuple:
            rel = os.path.relpath(path, image_base)
            return (len(rel.split(os.sep)), rel.lower())

        return {
            img_type_key: self._build_stem_lookup(entries, sort_key=_image_sort_key)
            for img_type_key, entries in collected.items()
        }

    def _index_flat_media_files(self, base_dir: str, extensions: tuple[str, ...]) -> StemLookupIndex:
        if not base_dir or not os.path.isdir(base_dir):
            return StemLookupIndex()
        entries: list[tuple[str, str]] = []
        try:
            for fname in os.listdir(base_dir):
                if fname.startswith("._") or fname.startswith("."):
                    continue
                stem, ext = os.path.splitext(fname)
                if ext.lower() not in extensions:
                    continue
                entries.append((stem, os.path.join(base_dir, fname)))
        except OSError:
            return StemLookupIndex()
        return self._build_stem_lookup(entries, sort_key=lambda path: os.path.basename(path).lower())

    def _build_stem_lookup(
        self,
        entries: list[tuple[str, str]],
        sort_key,
    ) -> StemLookupIndex:
        exact: dict[str, list[str]] = defaultdict(list)
        normalized_groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        skeleton_groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

        for stem, path in entries:
            stem_key = stem.casefold()
            exact[stem_key].append(path)
            normalized_groups[_normalize_media_match_key(stem)][stem_key].append(path)
            skeleton_groups[_normalize_media_match_skeleton(stem)][stem_key].append(path)

        for matches in exact.values():
            matches.sort(key=sort_key)

        def finalize(groups: dict[str, dict[str, list[str]]]) -> dict[str, list[str]]:
            resolved: dict[str, list[str]] = {}
            for key, grouped_paths in groups.items():
                if len(grouped_paths) != 1:
                    continue
                only_paths = next(iter(grouped_paths.values()))
                resolved[key] = sorted(only_paths, key=sort_key)
            return resolved

        return StemLookupIndex(
            exact=dict(exact),
            normalized=finalize(normalized_groups),
            skeleton=finalize(skeleton_groups),
        )

    def _candidate_stem_groups(
        self,
        base_stems: list[str],
        indexes: tuple[str, ...] | None = None,
    ) -> list[CandidateStemGroup]:
        groups: list[CandidateStemGroup] = []
        for stem in _unique_nonempty(base_stems):
            if indexes is None:
                candidates = (stem,)
            else:
                candidates = tuple([*(f"{stem}-{idx}" for idx in indexes), stem])
            groups.append(
                CandidateStemGroup(
                    exact=tuple(candidate.casefold() for candidate in candidates),
                    normalized=tuple(_normalize_media_match_key(candidate) for candidate in candidates),
                    skeleton=tuple(_normalize_media_match_skeleton(candidate) for candidate in candidates),
                )
            )
        return groups

    def _first_image_match(
        self,
        files: StemLookupIndex,
        candidate_groups: list[CandidateStemGroup],
    ) -> str:
        matches = self._best_media_matches(files, candidate_groups)
        if matches:
            return matches[0]
        return ""

    def _all_image_matches(
        self,
        files: StemLookupIndex,
        candidate_groups: list[CandidateStemGroup],
    ) -> list[str]:
        return self._best_media_matches(files, candidate_groups)

    def _best_media_matches(
        self,
        index: StemLookupIndex,
        candidate_groups: list[CandidateStemGroup],
    ) -> list[str]:
        for candidates in candidate_groups:
            exact_matches = self._collect_media_matches(index.exact, list(candidates.exact))
            if exact_matches:
                return exact_matches

            normalized_matches = self._collect_media_matches(index.normalized, list(candidates.normalized))
            if normalized_matches:
                return normalized_matches

            skeleton_matches = self._collect_media_matches(index.skeleton, list(candidates.skeleton))
            if skeleton_matches:
                return skeleton_matches
        return []

    def _collect_media_matches(self, index: dict[str, list[str]], lookup_keys: list[str]) -> list[str]:
        matches: list[str] = []
        seen: set[str] = set()
        for key in lookup_keys:
            for path in index.get(key, ()):
                if path in seen:
                    continue
                seen.add(path)
                matches.append(path)
        return matches

    def _yearful_media_bases(self, game: Game) -> list[str]:
        candidates: list[str] = []
        if game.launch_stem:
            candidates.append(game.launch_stem)
        if game.gamename:
            candidates.append(game.gamename)
        if game.release_year:
            candidates.append(f"{game.title} ({game.release_year})")
        return _unique_nonempty(candidates)

    def _image_candidate_bases(self, game: Game) -> list[str]:
        candidates: list[str] = []
        for stem in self._yearful_media_bases(game):
            candidates.append(_strip_name_year(stem))
            candidates.append(stem)
        candidates.append(_strip_name_year(game.title))
        candidates.append(game.title)
        return _unique_nonempty(candidates)

    def _collection_media_candidate_bases(self, game: Game) -> list[str]:
        candidates = self._yearful_media_bases(game)
        if not candidates and game.title:
            candidates.append(game.title)
        return _unique_nonempty(candidates)

    def _resolve_collection_media(self) -> None:
        """Load collection-level music and video files that match each game."""
        music_index = self._index_flat_media_files(
            self._config.abs_music_base(self.root),
            _COLLECTION_MUSIC_EXTENSIONS,
        )
        video_index = self._index_flat_media_files(
            self._config.abs_video_base(self.root),
            _COLLECTION_VIDEO_EXTENSIONS,
        )

        for game in self.games:
            candidate_groups = self._candidate_stem_groups(self._collection_media_candidate_bases(game))
            media_items = list(game.extras)
            seen_paths = {extra.path for extra in media_items}

            for path in self._all_image_matches(video_index, candidate_groups):
                if path in seen_paths:
                    continue
                stem, ext = os.path.splitext(os.path.basename(path))
                media_items.append(Extra(name=stem, path=path, kind="video", ext=ext.lstrip(".").lower()))
                seen_paths.add(path)

            for path in self._all_image_matches(music_index, candidate_groups):
                if path in seen_paths:
                    continue
                stem, ext = os.path.splitext(os.path.basename(path))
                media_items.append(Extra(name=stem, path=path, kind="audio", ext=ext.lstrip(".").lower()))
                seen_paths.add(path)

            game.extras = media_items

    def _resolve_extras(self) -> None:
        """Load videos, documents, and other Extras/ files for each game."""
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
