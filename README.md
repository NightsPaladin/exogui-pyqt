# eXoGUI

> **Unofficial** — This is a community project and is not affiliated with, endorsed by,
> or supported by the eXoDOS project or the retro-exo.com team.

A Python/PyQt6 GUI launcher for the [eXoDOS](https://www.retro-exo.com/) MS-DOS game collection and the eXoWin3x Windows 3.x collection.  
Works on **macOS** and **Linux**.

---

## Features

- **Multi-collection support** — eXoDOS and eXoWin3x, each with independent settings
- **Browse 7,000+ games** with instant search and multi-filter support
- **Three view modes** — List, Grid (box-art thumbnails), and Table
- **Game detail panel** — box art, description, screenshots carousel, videos, documents
- **Launch and install** games via dosbox-staging, dosbox-x, or ScummVM
- **Lite mode** — download individual game ZIPs on demand, either from a local/NAS source or directly via BitTorrent (requires aria2c)
- **Cancel in-progress downloads** — a Cancel button appears during any active download
- **18 built-in themes** (Dark, Light, Nord, Dracula, Rose Pine, Catppuccin, Cyberpunk, Tokyo Night, Gruvbox, Monokai, Everforest, Matrix, Ocean, One Dark, Ayu Dark, Kanagawa, Solarized Dark/Light) with auto System detection
- **Custom themes** — drop any `.json` file in `themes/` and it appears in the menu
- **Persistent settings** — window size, splitter position, and theme saved between sessions
- Filter by genre, year, rating, play mode, installed status, and preset categories

---

## Requirements

- Python 3.10 or newer
- PyQt6 (`pip install PyQt6`)
- [eXoDOS](https://www.retro-exo.com/) and/or eXoWin3x collection
- dosbox-staging and/or dosbox-x installed on your system
- **aria2c** *(optional — required for Lite mode torrent downloads)*

---

## Installation

1. Clone or copy this directory anywhere convenient (or inside your collection root):
   ```
   /path/to/eXoDOS/
   └── exogui-pyqt/      ← recommended location
   ```

2. Install PyQt6 if you haven't already:
   ```bash
   pip3 install PyQt6
   ```

3. Launch:
   ```bash
   cd /path/to/exogui-pyqt
   python3 main.py
   # or pass the collection root explicitly:
   python3 main.py /path/to/eXoDOS
   ```

   On macOS you can also double-click `exogui.command` in the collection root.

---

## Usage

### First run — Settings

Open **File → Settings** and configure each collection:

**Projects**

| Field | Description |
|-------|-------------|
| **Root Path** | Path to the collection root (eXoDOS or eXoWin3x) |
| **ZIP Source** *(Lite mode)* | Optional folder of pre-downloaded game ZIPs (local drive or NAS). Checked before falling back to torrent. |

**Emulators**

| Setting | Description |
|---------|-------------|
| **dosbox-staging** | Usually `dosbox-staging` (must be on `$PATH`) |
| **dosbox-x** | Usually `dosbox-x` |
| **dosbox-ece** | Path or command for DOSBox ECE |
| **scummvm** | Usually `scummvm` |

You can add multiple projects (eXoDOS + eXoWin3x simultaneously) using the **+ Add** button.

### Views

Click the **≡ / ⊞ / ☰** buttons at the top of the game list to switch between:
- **≡ List** — compact rows with box art thumbnail, title, year, and genre
- **⊞ Grid** — box-art cover grid
- **☰ Table** — sortable columns (Title, Year, Developer, Publisher, Genre)

### Lite Mode (download on demand)

If you have a collection without all game ZIPs pre-installed, eXoGUI can fetch them individually:

1. Ensure `aria2c` is installed (`brew install aria2c` on macOS, or via your Linux package manager).
2. The collection must have a torrent file and index at `eXo/util/aria/<name>.torrent` and `eXo/util/aria/index.txt`.
3. If `index.txt` is missing, generate it with the included utility:
   ```bash
   python3 generate_torrent_index.py /path/to/eXo/util/aria/eXoWin3x.torrent
   ```
4. Optionally set a **ZIP Source** path in Settings to copy from a local drive first (faster than torrent).
5. Click **⬇ Download & Install** on any uninstalled game. A **✕ Cancel** button appears while the download is active.

### Themes

**View → Theme** to switch themes. Your selection is saved automatically.  
To reset the window split: **View → Reset split to 50/50**.

### Custom Themes

Drop a `.json` file into `themes/` — see [`themes/README.md`](themes/README.md) for the full field reference.

---

## macOS notes

### Keyboard shortcuts during gameplay

When a game launches on macOS, eXoGUI automatically disables a set of system
keyboard shortcuts that would otherwise fire mid-game and steal input from
DOSBox:

| Keys suppressed | macOS action |
|-----------------|--------------|
| `Ctrl+Left` / `Ctrl+Right` | Move between Spaces |
| `Ctrl+Shift+Left` / `Ctrl+Shift+Right` | Move window to adjacent Space |
| `Ctrl+Up` / `Ctrl+Down` | Mission Control / App Exposé |
| `Ctrl+1` … `Ctrl+9` | Jump to Desktop 1–9 |

These shortcuts are restored automatically the moment you exit the game.

As part of applying the change, the **Dock briefly restarts** — you will see a
quick flash of the menu bar when the game launches and again when it exits.
This is expected and harmless; macOS requires a Dock restart to pick up the
updated shortcut preferences.

All DOSBox window controls continue to work normally (`Ctrl+F1` keymapper,
`Ctrl+F10` mouse capture, `Ctrl+Enter` fullscreen, etc.) — only the macOS
system shortcuts listed above are affected.

---



```
exogui-pyqt/
├── main.py              Entry point
├── core/
│   ├── project.py       ProjectConfig dataclass; built-in EXODOS and EXOWIN3X configs
│   ├── aria_index.py    Torrent index parser and aria2c command builder
│   ├── game_library.py  XML parser and game model
│   ├── image_cache.py   Async image/video-thumb loader
│   ├── launcher.py      dosbox / scummvm process management; Lite mode fetch + cancel
│   └── mac_shortcuts.py macOS-only: suppress conflicting system shortcuts during gameplay
├── gui/
│   ├── app_icon.py      Procedurally rendered application icon
│   ├── flow_layout.py   Wrapping flow layout widget
│   ├── game_detail.py   Right panel — art, metadata, screenshots, extras
│   ├── game_list.py     Left panel — list/grid/table views + filters
│   ├── main_window.py   Main window, menu bar, settings dialog
│   └── themes.py        Theme loader and switcher
└── themes/
    ├── README.md        Custom theme authoring guide
    ├── dark.json
    ├── light.json
    └── ...              (18 built-in themes)
```

The `generate_torrent_index.py` utility lives one level up (in the collection volume root) and is not part of the GUI itself.

---

## Emulator Setup

eXoGUI expects the following to be available on your `$PATH`:

| Emulator | Package (Ubuntu/Debian) | macOS (Homebrew) |
|----------|------------------------|-----------------|
| dosbox-staging | `dosbox-staging` | `brew install dosbox-staging` |
| dosbox-x | `dosbox-x` | `brew install dosbox-x` |
| ScummVM | `scummvm` | `brew install scummvm` |
| aria2c *(Lite mode)* | `aria2c` | `brew install aria2c` |

You can override the emulator command names in **File → Settings**.

---

## License

eXoGUI is released under the **MIT License**.  
eXoDOS and eXoWin3x and their content are the property of The eXo Team — see their documentation for licensing details.
