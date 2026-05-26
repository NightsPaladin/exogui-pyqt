#!/usr/bin/env python3
"""Generate README screenshots from the real PyQt widgets with mock data."""

from __future__ import annotations

import io
import json
import os
import random
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QAction, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow, QSplitter, QVBoxLayout, QWidget

from core.game_library import Extra, Game
from core.image_cache import ImageCache
from core.project import EXODOS
from gui import themes
from gui.app_icon import make_app_pixmap
from gui.game_detail import GameDetailPanel
from gui.game_list import GameListPanel
from gui.main_window import APP_NAME, SettingsDialog, WINDOW_H, WINDOW_W
from gui.pin_dialog import PinEntryDialog, PinSetupDialog, set_pin


REPO_ROOT = Path(__file__).resolve().parent
SCREENSHOTS_DIR = REPO_ROOT / "screenshots"
CANVAS_SIZE = (1296, 816)

_REGULAR_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_BOLD_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = _BOLD_FONT_CANDIDATES if bold else _REGULAR_FONT_CANDIDATES
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _qt_to_pil(pixmap: QPixmap) -> Image.Image:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int, int],
) -> None:
    words = text.split()
    lines: list[str] = []
    current = ""
    max_width = box[2] - box[0]
    for word in words:
        trial = f"{current} {word}".strip()
        if _measure(draw, trial, font)[0] <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    draw.multiline_text((box[0], box[1]), "\n".join(lines), font=font, fill=fill, spacing=4)


def _title_colors(seed_text: str) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
    rng = random.Random(seed_text)
    base = (rng.randint(32, 80), rng.randint(42, 118), rng.randint(88, 170))
    accent = (min(255, base[0] + 90), min(255, base[1] + 70), min(255, base[2] + 55))
    ink = (245, 242, 234)
    return base, accent, ink


def _make_cover(path: Path, title: str, subtitle: str) -> None:
    base, accent, ink = _title_colors(title)
    im = Image.new("RGBA", (360, 540), base + (255,))
    draw = ImageDraw.Draw(im)
    for y in range(im.height):
        ratio = y / max(1, im.height - 1)
        line = tuple(int(base[i] * (1 - ratio) + accent[i] * ratio) for i in range(3))
        draw.line((0, y, im.width, y), fill=line + (255,))
    for idx in range(0, im.height, 8):
        draw.line((0, idx, im.width, idx), fill=(255, 255, 255, 15))
    draw.rounded_rectangle((24, 24, im.width - 24, im.height - 24), radius=18, outline=(255, 255, 255, 60), width=2)
    draw.rectangle((44, 54, im.width - 44, 146), fill=(7, 10, 18, 110))
    draw.rectangle((44, 400, im.width - 44, 470), fill=(7, 10, 18, 135))
    _fit_text(draw, title.upper(), _font(36, bold=True), (58, 70, im.width - 58, 200), ink + (255,))
    _fit_text(draw, subtitle, _font(18), (58, 420, im.width - 58, 470), (230, 232, 238, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def _make_gallery_image(path: Path, title: str, label: str, *, width: int = 800, height: int = 500) -> None:
    base, accent, _ = _title_colors(f"{title}-{label}")
    im = Image.new("RGBA", (width, height), tuple(max(18, c - 18) for c in base) + (255,))
    draw = ImageDraw.Draw(im)
    horizon = height // 2 + 20
    draw.rectangle((0, 0, width, horizon), fill=tuple(min(255, c + 30) for c in accent) + (255,))
    draw.rectangle((0, horizon, width, height), fill=tuple(max(0, c - 10) for c in base) + (255,))
    for x in range(0, width, 46):
        tower = 120 + ((x // 23) % 5) * 28
        draw.rectangle((x, horizon - tower, x + 24, horizon), fill=(26, 29, 39, 240))
        for y in range(horizon - tower + 18, horizon - 10, 26):
            draw.rectangle((x + 5, y, x + 9, y + 6), fill=(255, 200, 120, 180))
            draw.rectangle((x + 14, y + 10, x + 18, y + 16), fill=(140, 215, 255, 180))
    hud = (30, 28, width - 30, 86)
    draw.rounded_rectangle(hud, radius=14, fill=(8, 10, 16, 150), outline=(255, 255, 255, 40), width=1)
    draw.text((50, 44), title, font=_font(22, bold=True), fill=(245, 247, 252, 255))
    draw.text((width - 180, 44), label.upper(), font=_font(18, bold=True), fill=(255, 220, 150, 255))
    panel = (width - 240, 116, width - 38, 248)
    draw.rounded_rectangle(panel, radius=16, fill=(7, 10, 16, 175), outline=(255, 255, 255, 48), width=1)
    draw.text((panel[0] + 18, panel[1] + 18), "OBJECTIVES", font=_font(18, bold=True), fill=(255, 255, 255, 225))
    draw.text((panel[0] + 18, panel[1] + 54), "- Reach the airlock\n- Restore power\n- Find the cargo lift", font=_font(16), fill=(212, 219, 226, 255), spacing=6)
    for idx in range(5):
        y = horizon + 38 + idx * 26
        draw.rounded_rectangle((38, y, 300, y + 18), radius=9, fill=(10, 12, 18, 155))
        draw.rectangle((42, y + 4, 42 + 34 * (idx + 3), y + 14), fill=accent + (230,))
    draw.rounded_rectangle((48, height - 92, width - 48, height - 38), radius=12, fill=(7, 10, 16, 140))
    draw.text((70, height - 78), "Comm Log: Sector secure. Proceed to docking ring delta.", font=_font(20), fill=(238, 240, 244, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def _make_doc_preview(path: Path) -> None:
    im = Image.new("RGBA", (220, 300), (238, 234, 226, 255))
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((12, 12, 208, 288), radius=14, fill=(252, 248, 242, 255), outline=(158, 150, 142, 255), width=2)
    draw.rectangle((30, 42, 188, 76), fill=(68, 78, 104, 255))
    draw.text((42, 48), "Reference Card", font=_font(20, bold=True), fill=(250, 251, 255, 255))
    for idx in range(7):
        y = 112 + idx * 22
        draw.line((34, y, 180, y), fill=(172, 167, 162, 255), width=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def _make_fallback_icon(path: Path) -> None:
    im = Image.new("RGBA", (256, 256), (24, 32, 56, 255))
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((18, 18, 238, 238), radius=42, fill=(36, 56, 92, 255), outline=(98, 130, 204, 255), width=4)
    draw.text((64, 74), "eXo", font=_font(56, bold=True), fill=(241, 245, 250, 255))
    draw.text((70, 150), "DOS", font=_font(42, bold=True), fill=(140, 214, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


@dataclass
class MockLibrary:
    games: list[Game]
    config = EXODOS

    def filter_installed(self) -> list[Game]:
        return [game for game in self.games if game.installed]

    def all_genres(self) -> list[str]:
        return sorted({genre.strip() for game in self.games for genre in game.genre.split(";") if genre.strip()})

    def all_years(self) -> list[int]:
        return sorted({game.release_year for game in self.games if game.release_year}, reverse=True)

    def all_ratings(self) -> list[str]:
        return sorted({game.rating for game in self.games if game.rating})

    def all_play_modes(self) -> list[str]:
        return sorted({mode.strip() for game in self.games for mode in game.play_mode.split(";") if mode.strip()})


@dataclass
class MockAssets:
    root: Path
    games: list[Game]


def _build_mock_assets(root: Path) -> MockAssets:
    _make_fallback_icon(root / "eXo" / "util" / "exodos.png")

    entries = [
        ("clockwork-harbor", "Clockwork Harbor", 1994, "Adventure; Puzzle", "Iron Lantern", "Softwave", "E", 4.7, True, "24 MB", "Single Player", "Playlist: Roland MT-32", "Commercial"),
        ("signal-lost", "Signal Lost", 1993, "Action; Sci-Fi", "Blue Mesa", "Blue Mesa", "T", 4.2, False, "31 MB", "Single Player", "Playlist: Sound Canvas", "Commercial"),
        ("vector-reign", "Vector Reign", 1991, "Arcade; Shooter", "Phase Shift", "Phase Shift", "E10+", 4.0, True, "", "Single Player; Hotseat Multiplayer", "Playlist: CGA Composite", "Freeware"),
        ("midnight-courier", "Midnight Courier", 1996, "Action; Racing", "Nocturne", "Northwind", "T", 4.1, False, "52 MB", "Single Player", "", "Commercial"),
        ("aether-circuit", "Aether Circuit", 1992, "Strategy; Simulation", "Signal Foundry", "Northwind", "E", 4.3, True, "", "Single Player", "Playlist: Printer Support", "Commercial"),
        ("neon-outpost", "Neon Outpost", 1990, "Action; Shooter", "LumaCore", "LumaCore", "T", 3.9, False, "18 MB", "Single Player", "", "Commercial"),
        ("nova-patrol", "Nova Patrol", 1989, "Action; Space", "Starglass", "Starglass", "E", 4.0, True, "", "Single Player", "", "Commercial"),
        ("dustrunner-2093", "Dustrunner 2093", 1995, "Racing; Combat", "Strata", "Strata", "M", 4.4, False, "77 MB", "Single Player", "Playlist: Gravis Ultrasound", "Commercial"),
        ("iron-frontier", "Iron Frontier", 1994, "Strategy; War", "HexHouse", "HexHouse", "T", 4.1, True, "", "Single Player; Network Multiplayer", "", "Commercial"),
        ("skyline-syndicate", "Skyline Syndicate", 1997, "Adventure; Thriller", "Nightjar", "Nightjar", "M", 4.5, False, "64 MB", "Single Player", "", "Commercial"),
        ("starlight-overdrive", "Starlight Overdrive", 1992, "Arcade; Music", "Prismline", "Prismline", "E", 3.8, True, "", "Single Player", "", "Freeware"),
        ("cipher-sector", "Cipher Sector", 1993, "Puzzle; Strategy", "Grid North", "Grid North", "E", 4.1, False, "14 MB", "Single Player", "", "Commercial"),
    ]

    media_root = root / "mock-media"
    games: list[Game] = []

    for slug, title, year, genre, developer, publisher, rating, community_rating, installed, download_size, play_mode, series, source in entries:
        game_dir = media_root / slug
        cover_path = game_dir / "cover.png"
        screenshots = [
            game_dir / "gallery-01.png",
            game_dir / "gallery-02.png",
            game_dir / "gallery-03.png",
        ]
        _make_cover(cover_path, title, f"{year}  -  {genre.split(';')[0]}")
        _make_gallery_image(screenshots[0], title, "Docking Ring")
        _make_gallery_image(screenshots[1], title, "Inventory")
        _make_gallery_image(screenshots[2], title, "Mission Brief")

        extras_dir = game_dir / "extras"
        extras_dir.mkdir(parents=True, exist_ok=True)
        info_path = extras_dir / "field-guide.txt"
        info_path.write_text("Mock documentation for screenshot generation.\n", encoding="utf-8")
        manual_path = extras_dir / "reference-card.pdf"
        manual_path.write_bytes(b"%PDF-1.4\n% mock screenshot asset\n")
        preview_path = extras_dir / "map-sheet.png"
        _make_doc_preview(preview_path)
        music_path = extras_dir / "title-theme.mp3"
        music_path.write_bytes(b"mock-audio")

        notes = (
            f"{title} is a fictional catalogue entry created for the README screenshots. "
            "It uses generated art and invented metadata, but the layout and controls come "
            "from the real PyQt application widgets."
        )

        game = Game(
            id=slug,
            title=title,
            sort_title=title,
            app_path=rf"eXo\\eXoDOS\\!dos\\{slug}\\{slug}.bat",
            root_folder=rf"eXo\\eXoDOS\\!dos\\{slug}",
            platform="MS-DOS",
            genre=genre,
            developer=developer,
            publisher=publisher,
            release_year=year,
            rating=rating,
            community_rating=community_rating,
            notes=notes,
            series=series,
            play_mode=play_mode,
            max_players=2 if "Multiplayer" in play_mode else 1,
            source=source,
            game_dir=slug.upper(),
            gamename=title,
            emulator="dosbox-staging",
            installed=installed,
            zip_present=installed,
            download_size_str=download_size,
            launch_stem=slug,
            image_paths={
                "cover": str(cover_path),
                "gallery": [str(path) for path in screenshots],
                "screenshots": [str(path) for path in screenshots],
            },
            compat_note="Mac audio stutters slightly during intro playback." if slug == "clockwork-harbor" else "",
            extras=[
                Extra("Field Guide", str(info_path), "document", "txt"),
                Extra("Reference Card", str(manual_path), "pdf", "pdf"),
                Extra("Map Sheet", str(preview_path), "image", "png"),
                Extra("Title Theme", str(music_path), "audio", "mp3"),
            ],
        )
        games.append(game)

    return MockAssets(root=root, games=games)


class ShowcaseMainWindow(QMainWindow):
    def __init__(
        self,
        library: MockLibrary,
        root: Path,
        cache: ImageCache,
        *,
        include_menu_bar: bool = True,
        platform_name: str = "linux",
    ):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} (Qt)")
        self._platform_name = platform_name
        self.resize(WINDOW_W, WINDOW_H)
        self._cache = cache
        self._library = library
        self._root = root
        if include_menu_bar:
            self._build_menu()
        self._build_status_bar()
        self._build_main_ui()
        self._set_status(f"eXoDOS: {len(library.games):,} games  -  {len(library.filter_installed()):,} installed")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(QAction("Settings...", self))
        file_menu.addSeparator()
        file_menu.addAction(QAction("Quit", self))

        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(QAction("Refresh library", self))
        theme_menu = view_menu.addMenu("Theme")
        for theme_name in ("System", "Dark", "Light", "Rose Pine"):
            action = QAction(theme_name, self)
            action.setCheckable(True)
            action.setChecked(theme_name == "Dark")
            theme_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(QAction("Reset split to 50/50", self))
        view_menu.addAction(QAction("Reset window size and position", self))

        tools_menu = self.menuBar().addMenu("Tools")
        tools_menu.addAction(QAction("Re-setup Collection...", self))

        help_menu = self.menuBar().addMenu("Help")
        help_menu.addAction(QAction("About", self))

    def _build_status_bar(self) -> None:
        self._status_label = QLabel("Ready")
        self.statusBar().addWidget(self._status_label)

    def _set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def _on_game_selected(self, game: Game) -> None:
        self._detail_panel.show_game(game)
        self._detail_panel.set_favorite(game.id == "clockwork-harbor")
        self._set_status(f"{game.title}  [{game.emulator_display}]")

    def _build_main_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1 if self._platform_name == "mac" else 2)
        self._list_panel = GameListPanel(self._library, self._cache, str(self._root))
        self._detail_panel = GameDetailPanel(self._cache, str(self._root))
        self._list_panel.game_selected.connect(self._on_game_selected)
        splitter.addWidget(self._list_panel)
        splitter.addWidget(self._detail_panel)
        splitter.setSizes([500, 680] if self._platform_name == "mac" else [540, 620])
        outer.addWidget(splitter)
        self.setCentralWidget(central)
        self._list_panel.select_first()


def _window_background(platform_name: str) -> Image.Image:
    top = (17, 21, 29) if platform_name == "mac" else (34, 38, 48)
    bottom = (25, 31, 40) if platform_name == "mac" else (43, 49, 61)
    canvas = Image.new("RGBA", CANVAS_SIZE, top + (255,))
    draw = ImageDraw.Draw(canvas)
    for y in range(CANVAS_SIZE[1]):
        ratio = y / max(1, CANVAS_SIZE[1] - 1)
        line = tuple(int(top[idx] * (1 - ratio) + bottom[idx] * ratio) for idx in range(3))
        draw.line((0, y, CANVAS_SIZE[0], y), fill=line + (255,))
    return canvas


def _make_shadow(
    size: tuple[int, int],
    *,
    radius: int = 22,
    alpha: int = 175,
    padding: tuple[int, int] = (40, 28),
    blur: int = 18,
) -> Image.Image:
    pad_x, pad_y = padding
    shadow = Image.new("RGBA", (size[0] + pad_x * 2, size[1] + pad_y * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    draw.rounded_rectangle((pad_x, pad_y, pad_x + size[0], pad_y + size[1]), radius=radius, fill=(0, 0, 0, alpha))
    return shadow.filter(ImageFilter.GaussianBlur(blur))


def _linux_icon() -> Image.Image:
    return _qt_to_pil(make_app_pixmap(18))


def _draw_mac_traffic_lights(draw: ImageDraw.ImageDraw) -> None:
    colors = ((255, 95, 87), (255, 189, 46), (40, 200, 64))
    top = 15
    diameter = 12
    for idx, color in enumerate(colors):
        left = 15 + idx * 20
        draw.ellipse(
            (left, top, left + diameter, top + diameter),
            fill=color + (255,),
        )


def _draw_linux_window_buttons(draw: ImageDraw.ImageDraw, frame_width: int) -> None:
    button_fill = (78, 82, 92, 255)
    symbol = (240, 242, 247, 255)
    for idx in range(3):
        x = frame_width - 78 + idx * 22
        draw.rounded_rectangle((x, 6, x + 18, 24), radius=4, fill=button_fill)

        cx = x + 9
        if idx == 0:
            draw.line((cx - 4, 16, cx + 4, 16), fill=symbol, width=2)
        elif idx == 1:
            draw.rectangle((cx - 4, 11, cx + 4, 19), outline=symbol, width=1)
        else:
            draw.line((cx - 4, 11, cx + 4, 19), fill=symbol, width=2)
            draw.line((cx - 4, 19, cx + 4, 11), fill=symbol, width=2)


def _frame_widget(content: Image.Image, title: str, platform_name: str) -> Image.Image:
    titlebar_height = 44 if platform_name == "mac" else 34
    radius = 14 if platform_name == "mac" else 10
    frame = Image.new("RGBA", (content.width, content.height + titlebar_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(frame)

    shadow = _make_shadow(
        frame.size,
        radius=radius,
        alpha=150 if platform_name == "mac" else 175,
        padding=(48, 36) if platform_name == "mac" else (40, 28),
        blur=24 if platform_name == "mac" else 18,
    )
    framed = Image.new("RGBA", shadow.size, (0, 0, 0, 0))
    framed.alpha_composite(shadow, (0, 0))

    window_origin = (48, 36) if platform_name == "mac" else (40, 28)
    if platform_name == "mac":
        outer = (42, 43, 47, 255)
        title_top = (62, 64, 71, 255)
        title_bottom = (51, 52, 58, 255)
        body_fill = (29, 30, 34, 255)
        draw.rounded_rectangle((0, 0, frame.width, frame.height), radius=radius, fill=outer)
        for y in range(titlebar_height):
            ratio = y / max(1, titlebar_height - 1)
            line = tuple(int(title_top[idx] * (1 - ratio) + title_bottom[idx] * ratio) for idx in range(3))
            draw.line((1, y, frame.width - 2, y), fill=line + (255,))
        draw.rounded_rectangle((1, titlebar_height - 1, frame.width - 2, frame.height - 2), radius=radius - 1, fill=body_fill)
        draw.line((1, titlebar_height, frame.width - 2, titlebar_height), fill=(102, 106, 116, 120), width=1)
        draw.rounded_rectangle((1, 1, frame.width - 2, frame.height - 2), radius=radius - 1, outline=(255, 255, 255, 16), width=1)
        draw.line((2, 1, frame.width - 3, 1), fill=(255, 255, 255, 28), width=1)

        _draw_mac_traffic_lights(draw)

        draw.text((frame.width // 2, 15), title, font=_font(13, bold=True), fill=(236, 238, 242, 230), anchor="ma")
    else:
        draw.rounded_rectangle((0, 0, frame.width, frame.height), radius=radius, fill=(60, 64, 72, 255))
        draw.rectangle((0, titlebar_height, frame.width, frame.height), fill=(24, 24, 28, 255))
        draw.rectangle((0, titlebar_height - 1, frame.width, titlebar_height), fill=(108, 111, 120, 125))
        icon = _linux_icon()
        frame.alpha_composite(icon, (12, 8))
        draw.text((38, 9), title, font=_font(14, bold=True), fill=(239, 241, 245, 255))
        _draw_linux_window_buttons(draw, frame.width)

    frame.alpha_composite(content, (0, titlebar_height))
    mask = Image.new("L", frame.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, frame.width, frame.height), radius=radius, fill=255)
    framed.alpha_composite(Image.composite(frame, Image.new("RGBA", frame.size, (0, 0, 0, 0)), mask), window_origin)
    return framed


def _center_on_canvas(framed: Image.Image, platform_name: str) -> Image.Image:
    canvas = _window_background(platform_name)
    x = (CANVAS_SIZE[0] - framed.width) // 2
    y = max(22, (CANVAS_SIZE[1] - framed.height) // 2)
    canvas.alpha_composite(framed, (x, y))
    return canvas


def _compose_modal_scene(
    background: Image.Image,
    dialog_frame: Image.Image,
    *,
    platform_name: str,
    blur_radius: int = 6,
) -> Image.Image:
    scene = background.filter(ImageFilter.GaussianBlur(10 if platform_name == "mac" else blur_radius))
    overlay = (8, 10, 14, 92) if platform_name == "mac" else (5, 7, 10, 108)
    scene.alpha_composite(Image.new("RGBA", CANVAS_SIZE, overlay))
    x = (CANVAS_SIZE[0] - dialog_frame.width) // 2
    y = (CANVAS_SIZE[1] - dialog_frame.height) // 2 - (18 if platform_name == "mac" else 8)
    scene.alpha_composite(dialog_frame, (x, y))
    return scene


def _capture(widget: QWidget, *, size: tuple[int, int] | None = None) -> Image.Image:
    app = QApplication.instance()
    assert app is not None
    if size is not None:
        widget.resize(*size)
    widget.show()
    for _ in range(4):
        app.processEvents()
    pixmap = widget.grab()
    widget.hide()
    return _qt_to_pil(pixmap)


def _warm_cache(cache: ImageCache, games: list[Game]) -> None:
    paths: list[str] = []
    for game in games:
        cover = game.primary_cover_path
        if cover:
            paths.append(cover)
        gallery = game.image_paths.get("gallery", [])
        if isinstance(gallery, list):
            paths.extend(str(path) for path in gallery)
    for path in paths:
        cache.get(path)
        cache.get(path, scaled_to=(60, 80))
        cache.get(path, scaled_to=(88, 60))
        cache.get(path, scaled_to=(640, 400))
    cache._pool.waitForDone()
    app = QApplication.instance()
    assert app is not None
    for _ in range(4):
        app.processEvents()


def _create_settings_dialog(settings_path: Path, platform_name: str) -> SettingsDialog:
    from PyQt6.QtCore import QSettings

    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    projects = [
        {"id": "exodos", "root": "/Volumes/eXo/eXoDOS"},
        {"id": "exowin3x", "root": "/Volumes/eXo/eXoWin3x"},
    ]
    settings.setValue("projects", json.dumps(projects))
    settings.setValue("project_exodos/zip_source_path", "/Volumes/RetroNAS/eXoZIPs")
    settings.setValue("project_exodos/torrent_path", "/Volumes/eXo/eXo/util/aria/eXoDOS.torrent")
    settings.setValue("project_exowin3x/zip_source_path", "/Volumes/RetroNAS/eXoZIPs/Win3x")
    settings.setValue("project_exowin3x/torrent_path", "/Volumes/eXo/eXo/util/aria/eXoWin3x.torrent")
    settings.setValue("project_exodos/show_mature", False)
    settings.setValue("music_autoplay", "true")
    set_pin(settings, "2580")
    settings.setValue(
        "emulators",
        json.dumps(
            [
                {"name": "dosbox-staging", "command": "dosbox-staging"},
                {"name": "dosbox-x", "command": "dosbox-x"},
                {"name": "dosbox-ece", "command": "/Applications/DOSBox ECE.app"},
                {"name": "scummvm", "command": "scummvm"},
            ]
        ),
    )

    with patch.object(sys, "platform", "darwin" if platform_name == "mac" else "linux"):
        dialog = SettingsDialog(settings)
    dialog.resize(832 if platform_name == "mac" else 780, 680 if platform_name == "mac" else 560)
    return dialog


def _create_pin_setup_dialog() -> PinSetupDialog:
    dialog = PinSetupDialog()
    dialog._pin1.setText("2580")
    dialog._pin2.setText("2580")
    return dialog


def _create_pin_entry_dialog(settings_path: Path) -> PinEntryDialog:
    from PyQt6.QtCore import QSettings

    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    set_pin(settings, "2580")
    dialog = PinEntryDialog(settings)
    dialog._edit.setText("2580")
    return dialog


@contextmanager
def _platform_style(app: QApplication, platform_name: str):
    original_style = app.style().objectName()
    if platform_name == "linux":
        app.setStyle("Fusion")
    else:
        app.setStyle("macOS")
    try:
        yield
    finally:
        app.setStyle(original_style)


@contextmanager
def _temporary_style(app: QApplication, style_name: str):
    original_style = app.style().objectName()
    app.setStyle(style_name)
    try:
        yield
    finally:
        app.setStyle(original_style)


def _save(image: Image.Image, name: str) -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    image.save(SCREENSHOTS_DIR / name)
    print(f"wrote {name}")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    themes.set_theme("Dark", app)

    with tempfile.TemporaryDirectory(prefix="exogui-screens-") as tmp:
        tmp_root = Path(tmp)
        assets = _build_mock_assets(tmp_root)
        cache = ImageCache(max_size=256)
        _warm_cache(cache, assets.games)

        for platform_name in ("mac", "linux"):
            with _platform_style(app, platform_name):
                library = MockLibrary(assets.games)
                main_window = ShowcaseMainWindow(
                    library,
                    assets.root,
                    cache,
                    include_menu_bar=platform_name != "mac",
                    platform_name=platform_name,
                )
                main_content = _capture(
                    main_window,
                    size=(1188, 744) if platform_name == "mac" else (1160, 720),
                )
                main_scene = _center_on_canvas(_frame_widget(main_content, f"{APP_NAME} (Qt)", platform_name), platform_name)
                _save(main_scene, f"exogui-{platform_name}-main.png")
                main_window.close()

                if platform_name == "mac":
                    with _temporary_style(app, "Fusion"):
                        settings_dialog = _create_settings_dialog(tmp_root / f"{platform_name}-settings.ini", platform_name)
                        settings_content = _capture(settings_dialog)
                        settings_dialog.close()
                else:
                    settings_dialog = _create_settings_dialog(tmp_root / f"{platform_name}-settings.ini", platform_name)
                    settings_content = _capture(settings_dialog)
                    settings_dialog.close()
                settings_frame = _frame_widget(settings_content, "Settings", platform_name)
                if platform_name == "mac":
                    settings_scene = Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 255))
                    sx = (CANVAS_SIZE[0] - settings_frame.width) // 2
                    sy = max(8, (CANVAS_SIZE[1] - settings_frame.height) // 2)
                    settings_scene.alpha_composite(settings_frame, (sx, sy))
                else:
                    settings_scene = _compose_modal_scene(
                        main_scene,
                        settings_frame,
                        platform_name=platform_name,
                    )
                _save(settings_scene, f"exogui-{platform_name}-settings.png")

                setup_dialog = _create_pin_setup_dialog()
                setup_content = _capture(setup_dialog)
                setup_scene = _compose_modal_scene(
                    main_scene,
                    _frame_widget(setup_content, "Set Parental Control PIN", platform_name),
                    platform_name=platform_name,
                )
                _save(setup_scene, f"exogui-{platform_name}-first-launch.png")
                setup_dialog.close()

                entry_dialog = _create_pin_entry_dialog(tmp_root / f"{platform_name}-pin.ini")
                entry_content = _capture(entry_dialog)
                entry_scene = _compose_modal_scene(
                    main_scene,
                    _frame_widget(entry_content, "Parental Control", platform_name),
                    platform_name=platform_name,
                )
                _save(entry_scene, f"exogui-{platform_name}-pin-entry.png")
                entry_dialog.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
