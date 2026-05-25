"""
pin_dialog.py — Parental-control PIN helpers and dialogs.

One global 4-digit PIN protects the "Include adult/mature titles" setting.
The PIN is stored as a SHA-256 hash in QSettings under parental_control/pin_hash.
Once verified in a session the app stays unlocked until restart.

To reset a forgotten PIN, run:  python3 main.py --reset-pin
"""

from __future__ import annotations

import hashlib

from PyQt6.QtCore import Qt, QRegularExpression, QSettings
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QDialogButtonBox, QMessageBox, QWidget,
)

_PIN_HASH_KEY   = "parental_control/pin_hash"
_SETUP_DONE_KEY = "parental_control/setup_done"

_session_unlocked: bool = False


# ── public state helpers ──────────────────────────────────────────────────────

def has_pin(settings: QSettings) -> bool:
    return bool(settings.value(_PIN_HASH_KEY, ""))


def set_pin(settings: QSettings, pin: str) -> None:
    settings.setValue(_PIN_HASH_KEY, hashlib.sha256(pin.encode()).hexdigest())


def clear_pin(settings: QSettings) -> None:
    settings.remove(_PIN_HASH_KEY)
    settings.remove(_SETUP_DONE_KEY)


def verify_pin(settings: QSettings, pin: str) -> bool:
    stored = settings.value(_PIN_HASH_KEY, "")
    if not stored:
        return True
    return hashlib.sha256(pin.encode()).hexdigest() == stored


def is_session_unlocked() -> bool:
    return _session_unlocked


def unlock_session() -> None:
    global _session_unlocked
    _session_unlocked = True


def is_first_launch(settings: QSettings) -> bool:
    return not settings.value(_SETUP_DONE_KEY, False, type=bool)


def mark_first_launch_done(settings: QSettings) -> None:
    settings.setValue(_SETUP_DONE_KEY, True)


# ── shared PIN input widget ───────────────────────────────────────────────────

class _PinEdit(QLineEdit):
    """Masked, digits-only, 4-character PIN field."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.setMaxLength(4)
        self.setPlaceholderText("····")
        self.setFixedWidth(80)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"\d{0,4}"))
        )


# ── dialogs ───────────────────────────────────────────────────────────────────

class PinEntryDialog(QDialog):
    """Ask the user to enter the existing PIN to unlock adult content."""

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Parental Control")
        self.setModal(True)
        self.setFixedWidth(300)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Enter your parental control PIN to continue:"))

        row = QHBoxLayout()
        self._edit = _PinEdit()
        self._edit.returnPressed.connect(self._try_accept)
        row.addStretch()
        row.addWidget(self._edit)
        row.addStretch()
        layout.addLayout(row)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._try_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _try_accept(self) -> None:
        if not verify_pin(self._settings, self._edit.text()):
            QMessageBox.warning(self, "Incorrect PIN", "That PIN is incorrect.")
            self._edit.clear()
            self._edit.setFocus()
            return
        self.accept()


class PinSetupDialog(QDialog):
    """Let the parent choose a new 4-digit PIN."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set Parental Control PIN")
        self.setModal(True)
        self.setFixedWidth(320)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Choose a 4-digit PIN to protect the adult-content\n"
            "setting. You will need it to enable adult titles."
        ))

        r1 = QHBoxLayout()
        lbl1 = QLabel("PIN:")
        lbl1.setFixedWidth(64)
        self._pin1 = _PinEdit()
        r1.addWidget(lbl1)
        r1.addWidget(self._pin1)
        r1.addStretch()
        layout.addLayout(r1)

        r2 = QHBoxLayout()
        lbl2 = QLabel("Confirm:")
        lbl2.setFixedWidth(64)
        self._pin2 = _PinEdit()
        self._pin2.returnPressed.connect(self._try_accept)
        r2.addWidget(lbl2)
        r2.addWidget(self._pin2)
        r2.addStretch()
        layout.addLayout(r2)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._try_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    @property
    def pin(self) -> str:
        return self._pin1.text()

    def _try_accept(self) -> None:
        p1 = self._pin1.text()
        p2 = self._pin2.text()
        if len(p1) != 4:
            QMessageBox.warning(self, "Invalid PIN", "PIN must be exactly 4 digits.")
            self._pin1.setFocus()
            return
        if p1 != p2:
            QMessageBox.warning(self, "Mismatch", "PINs do not match. Please try again.")
            self._pin2.clear()
            self._pin2.setFocus()
            return
        self.accept()
