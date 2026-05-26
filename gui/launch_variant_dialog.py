"""
launch_variant_dialog.py — Pre-launch choice dialog for games with multiple
engine or configuration options (parsed from exception.bsh files).
"""

from __future__ import annotations

import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Non-English language keywords that can appear in exception.bsh echo labels
_NON_ENGLISH_RE = re.compile(
    r'\b(french|german|spanish|italian|dutch|polish|russian|czech|portuguese'
    r'|japanese|chinese|korean|danish|swedish|norwegian|finnish|hungarian'
    r'|português|español|français|deutsch|italiano|nederlands)\b',
    re.IGNORECASE,
)
_ENGLISH_RE = re.compile(r'\b(english)\b', re.IGNORECASE)


def _is_non_english(label: str) -> bool:
    """Return True if the label explicitly mentions a non-English language."""
    return bool(_NON_ENGLISH_RE.search(label)) and not _ENGLISH_RE.search(label)


def _sort_key(variant) -> tuple:
    """
    Sort priority: English ScummVM → English DOSBox → non-English (any).
    Within each group original order is preserved via stable sort.
    """
    non_eng = _is_non_english(getattr(variant, "label", ""))
    is_scummvm = getattr(variant, "engine", "") == "scummvm"
    # (non_english, not_scummvm) → smaller tuple = higher priority
    return (1 if non_eng else 0, 0 if is_scummvm else 1)


class LaunchVariantDialog(QDialog):
    """
    Modal dialog that asks the user which launch variant to use.

    After exec(), check ``selected_index`` (≥ 0 on Accept, -1 on Cancel/close).
    The index corresponds to the position in the *original* variants list
    passed to the constructor (not the display order after English-first sort).
    """

    def __init__(self, game_title: str, variants: list, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Choose Launch Option")
        self.setModal(True)
        self.setMinimumWidth(440)
        self.selected_index: int = -1

        # Sort: English ScummVM first, then English DOSBox, then non-English.
        # Python's sort is stable so ties preserve original order.
        indexed = list(enumerate(variants))
        indexed.sort(key=lambda iv: _sort_key(iv[1]))
        # _ordered_indices[display_position] → original_list_index
        self._ordered_indices = [orig_i for orig_i, _ in indexed]
        sorted_variants = [v for _, v in indexed]

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 14, 16, 14)

        header = QLabel(
            f"<b>{game_title}</b> supports multiple launch options.<br>"
            "Choose which one to use:"
        )
        header.setWordWrap(True)
        header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(header)

        # Compact options area
        options_widget = QWidget(self)
        options_layout = QVBoxLayout(options_widget)
        options_layout.setSpacing(1)
        options_layout.setContentsMargins(0, 4, 0, 4)

        self._group = QButtonGroup(self)
        first_english = True

        for display_i, v in enumerate(sorted_variants):
            raw_label = getattr(v, "label", "") or f"Option {display_i + 1}"
            engine = getattr(v, "engine", "")
            non_eng = _is_non_english(raw_label)

            # Build HTML for the radio label
            label_html = f"<span>{raw_label}</span>"
            if engine:
                label_html += f"&nbsp;<span style='color:#888;font-size:11px;'>[{engine}]</span>"
            if non_eng:
                label_html += (
                    "&nbsp;<span style='color:#d9822b;font-size:11px;'>"
                    "&#x26A0;&nbsp;not in English</span>"
                )
            else:
                label_html += (
                    "&nbsp;<span style='color:#5a9;font-size:11px;'>[English]</span>"
                )

            btn = QRadioButton(self)
            rich_label = _ClickableLabel(label_html, btn, self)
            rich_label.setTextFormat(Qt.TextFormat.RichText)
            rich_label.setWordWrap(True)
            rich_label.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
            )

            row = QWidget(self)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(2, 1, 2, 1)
            row_layout.setSpacing(4)
            row_layout.addWidget(btn)
            row_layout.addWidget(rich_label, 1)

            self._group.addButton(btn, display_i)
            options_layout.addWidget(row)

            if first_english and not non_eng:
                btn.setChecked(True)
                first_english = False

        if first_english:
            # All options non-English (unlikely); check the first one
            first_btn = self._group.button(0)
            if first_btn:
                first_btn.setChecked(True)

        layout.addWidget(options_widget)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        display_idx = self._group.checkedId()
        if display_idx < 0:
            display_idx = 0
        self.selected_index = self._ordered_indices[display_idx]
        self.accept()


class _ClickableLabel(QLabel):
    """QLabel that selects an associated radio button when clicked."""

    def __init__(self, text: str, radio: QRadioButton, parent: QWidget | None = None):
        super().__init__(text, parent)
        self._radio = radio

    def mousePressEvent(self, ev) -> None:  # type: ignore[override]
        self._radio.setChecked(True)
        super().mousePressEvent(ev)
