"""
favorites.py — In-memory store for per-project favorite game IDs.

Persistence is handled by the caller (MainWindow) which serialises the store
to/from exogui.ini via QSettings so that all user data stays in one file.
"""

from __future__ import annotations


class FavoritesStore:
    def __init__(self, data: dict | None = None) -> None:
        """Initialise from a dict of {project_id: [game_id, ...]}."""
        self._data: dict[str, list[str]] = {}
        if data:
            for k, v in data.items():
                if isinstance(v, list):
                    self._data[k] = [s for s in v if isinstance(s, str)]

    # ── public ────────────────────────────────────────────────────────────────

    def is_favorite(self, project_id: str, game_id: str) -> bool:
        return bool(game_id) and game_id in self._data.get(project_id, [])

    def toggle(self, project_id: str, game_id: str) -> bool:
        """Toggle favorite status.  Returns the new state (True = now a favorite)."""
        if not game_id:
            return False
        ids = self._data.setdefault(project_id, [])
        if game_id in ids:
            ids.remove(game_id)
            return False
        ids.append(game_id)
        return True

    def get_set(self, project_id: str) -> set[str]:
        return set(self._data.get(project_id, []))

    def to_dict(self) -> dict:
        """Return a plain dict suitable for JSON serialisation."""
        return {k: list(v) for k, v in self._data.items()}
