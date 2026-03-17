"""
image_cache.py — Asynchronous image loading with an in-memory LRU cache.

Images are loaded off the main thread using QRunnable workers so the UI
never blocks on disk I/O.  The cache keeps the most-recently-used
*max_size* QPixmap objects in memory.

Also provides get_video_thumb() which extracts a preview frame from a video
file using ffmpeg (if available) and caches the result as a PNG on disk.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections import OrderedDict
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPixmap


# Disk cache directory for extracted video thumbnails
_THUMB_CACHE_DIR = os.path.expanduser("~/.cache/exogui/thumbs")


# ── workers ───────────────────────────────────────────────────────────────────

class _LoadSignals(QObject):
    loaded = pyqtSignal(str, QPixmap)   # path, pixmap (null on failure)


class _LoadWorker(QRunnable):
    def __init__(self, path: str, signals: _LoadSignals):
        super().__init__()
        self.path = path
        self.signals = signals
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        pm = QPixmap(self.path)
        self.signals.loaded.emit(self.path, pm)


class _VideoThumbWorker(QRunnable):
    """Extract a single frame from a video using ffmpeg, then signal the PNG."""

    def __init__(self, video_path: str, thumb_path: str, signals: _LoadSignals):
        super().__init__()
        self._video_path = video_path
        self._thumb_path = thumb_path
        self.signals = signals
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        try:
            os.makedirs(os.path.dirname(self._thumb_path), exist_ok=True)
            subprocess.run(
                [
                    "ffmpeg", "-i", self._video_path,
                    "-ss", "00:00:01", "-vframes", "1",
                    "-vf", "scale=160:-1", "-y", self._thumb_path,
                ],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass
        pm = QPixmap(self._thumb_path) if os.path.exists(self._thumb_path) else QPixmap()
        self.signals.loaded.emit(self._video_path, pm)


# ── cache ─────────────────────────────────────────────────────────────────────

class ImageCache(QObject):
    """
    Thread-safe async image loader with LRU eviction.

    Usage::

        cache = ImageCache(max_size=400)
        cache.get(path, callback)   # callback(path, QPixmap) called on main thread
    """

    image_ready = pyqtSignal(str, QPixmap)   # path, pixmap

    def __init__(self, max_size: int = 400, parent=None):
        super().__init__(parent)
        self._max_size = max_size
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._in_flight: set[str] = set()
        self._callbacks: dict[str, list[Callable]] = {}
        self._pool = QThreadPool.globalInstance()
        self.image_ready.connect(self._on_loaded)

    # ── public ────────────────────────────────────────────────────────────────

    def get(self, path: str, callback: Optional[Callable[[str, QPixmap], None]] = None,
            scaled_to: Optional[tuple[int, int]] = None) -> Optional[QPixmap]:
        """
        Return a QPixmap for *path* if already cached, else schedule a load.
        *callback* is called with (path, pixmap) on the main thread when ready.
        *scaled_to* (w, h): if provided, return/cache a scaled copy.
        """
        cache_key = f"{path}@{scaled_to}" if scaled_to else path

        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            pm = self._cache[cache_key]
            if callback:
                callback(cache_key, pm)
            return pm

        if callback:
            self._callbacks.setdefault(cache_key, []).append(callback)

        if cache_key not in self._in_flight:
            self._in_flight.add(cache_key)
            signals = _LoadSignals()
            signals.loaded.connect(
                lambda p, pm, key=cache_key, s=scaled_to: self._on_raw_loaded(key, pm, s)
            )
            worker = _LoadWorker(path, signals)
            self._pool.start(worker)

        return None

    def get_video_thumb(self, video_path: str,
                        callback: Optional[Callable[[str, QPixmap], None]] = None,
                        scaled_to: Optional[tuple[int, int]] = None) -> Optional[QPixmap]:
        """
        Return a thumbnail QPixmap for *video_path*.

        If ffmpeg has already extracted a cached PNG, loads it immediately.
        Otherwise schedules ffmpeg extraction in the background thread pool.
        *callback* is called with (cache_key, pixmap) on the main thread.
        """
        h = hashlib.md5(video_path.encode()).hexdigest()
        thumb_path = os.path.join(_THUMB_CACHE_DIR, f"{h}.png")
        cache_key = f"video:{h}@{scaled_to}"

        # Already in memory cache
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            pm = self._cache[cache_key]
            if callback:
                callback(cache_key, pm)
            return pm

        if callback:
            self._callbacks.setdefault(cache_key, []).append(callback)

        if cache_key not in self._in_flight:
            self._in_flight.add(cache_key)
            signals = _LoadSignals()

            if os.path.exists(thumb_path):
                # PNG already on disk — just load it
                signals.loaded.connect(
                    lambda p, pm, key=cache_key, s=scaled_to:
                        self._on_raw_loaded(key, pm, s)
                )
                worker = _LoadWorker(thumb_path, signals)
            else:
                # Need to extract via ffmpeg first
                signals.loaded.connect(
                    lambda p, pm, key=cache_key, s=scaled_to:
                        self._on_raw_loaded(key, pm, s)
                )
                worker = _VideoThumbWorker(video_path, thumb_path, signals)

            self._pool.start(worker)

        return None

    def prefetch(self, paths: list[str]) -> None:
        for p in paths:
            self.get(p)

    def clear(self) -> None:
        self._cache.clear()
        self._in_flight.clear()
        self._callbacks.clear()

    # ── private ───────────────────────────────────────────────────────────────

    def _on_raw_loaded(self, cache_key: str, pm: QPixmap,
                       scaled_to: Optional[tuple[int, int]]) -> None:
        if scaled_to and not pm.isNull():
            pm = pm.scaled(scaled_to[0], scaled_to[1],
                           Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self._store(cache_key, pm)
        self.image_ready.emit(cache_key, pm)
        for cb in self._callbacks.pop(cache_key, []):
            cb(cache_key, pm)
        self._in_flight.discard(cache_key)

    def _on_loaded(self, path: str, pm: QPixmap) -> None:
        pass  # handled in _on_raw_loaded via direct connection

    def _store(self, key: str, pm: QPixmap) -> None:
        self._cache[key] = pm
        self._cache.move_to_end(key)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
