"""
overlay.py

Click-through оверлей с ценами поверх игры. PySide6 (Qt).
Qt.WindowTransparentForInput = OS-level click-through (WS_EX_TRANSPARENT).
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget

from scan_engine import PriceRow, NOT_FOUND_MARKER

# Паттерн для пасхалки 🎰: уникалки и рандомная валюта
_SLOT_MACHINE = "🎰"


def _is_slot_machine(name: str) -> bool:
    """True для наград с элементом рандома (уникальные предметы, случайная валюта)."""
    return "unique" in name or ("random" in name and "currency" in name)


@dataclass
class OverlayConfig:
    offset_x: int = 10
    font_family: str = "Segoe UI"
    font_size: int = 12
    overlay_width: int = 80          # сужено вдвое (было 260)
    bg_alpha: int = 180                # полупрозрачный тёмный фон (0=прозрачный, 255=непрозрачный)
    fg_found: str = "#FFD700"
    fg_not_found: str = "#888888"
    fg_reading: str = "#666666"
    topmost_refresh_ms: int = 1000


class PriceOverlay(QWidget):
    def __init__(self, region: tuple[int, int, int, int], config: OverlayConfig | None = None):
        super().__init__()
        self._region = region
        self._config = config or OverlayConfig()
        self._items: list[tuple[int, int, str, str]] = []

        x, y, w, h = region
        self.setGeometry(x + w + self._config.offset_x, y, self._config.overlay_width, max(1, h))

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_topmost)
        self._timer.start(self._config.topmost_refresh_ms)

    def _refresh_topmost(self) -> None:
        if self.isVisible():
            self.raise_()

    def update_state(self, rows: list[PriceRow], confirmed: bool, reading: bool) -> None:
        if not rows and not reading:
            self.hide()
            return
        self._items = self._build_items(rows, reading)
        if not self.isVisible():
            self.show()
        self.update()

    def _build_items(self, rows: list[PriceRow], reading: bool) -> list[tuple[int, int, str, str]]:
        if reading and not rows:
            return [(8, 8, "чтение...", self._config.fg_reading)]
        items = []
        for row in rows:
            text, color = self._format_row(row)
            y = max(0, row.center_y - self._config.font_size)
            items.append((8, y, text, color))
        return items

    def _format_row(self, row: PriceRow) -> tuple[str, str]:
        # ВАЖНО: пасхалку проверяем ДО has_price — slot machine предметы
        # (уникалки, случайная валюта) не торгуются на poe.ninja и всегда
        # имеют has_price=False, но должны показывать 🎰, а не "—".
        if _is_slot_machine(row.name):
            return (_SLOT_MACHINE, self._config.fg_found)
        if not row.has_price:
            return (NOT_FOUND_MARKER, self._config.fg_not_found)
        if row.divine_value >= 1:
            return (f"{row.divine_value:.2f} div", self._config.fg_found)
        return (f"{row.exalted_value:.1f} ex", self._config.fg_found)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Фон: полностью прозрачный (bg_alpha=0) — виден только текст
        if self._config.bg_alpha > 0:
            bg = QColor(0, 0, 0, self._config.bg_alpha)
            painter.fillRect(self.rect(), bg)

        font = QFont(self._config.font_family, self._config.font_size)
        font.setBold(True)
        painter.setFont(font)

        for x, y, text, color in self._items:
            # Тень — читаемость на любом фоне игры
            painter.setPen(QColor(0, 0, 0, 180))
            painter.drawText(x + 1, y + self._config.font_size + 1, text)
            painter.setPen(QColor(color))
            painter.drawText(x, y + self._config.font_size, text)
