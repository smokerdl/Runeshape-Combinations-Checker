"""
overlay.py

Click-through оверлей с ценами поверх игры. PySide6 (Qt).
Qt.WindowTransparentForInput = OS-level click-through (WS_EX_TRANSPARENT).
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap, QFontMetrics
from PySide6.QtWidgets import QWidget

from scan_engine import PriceRow, NOT_FOUND_MARKER

# Маркер для отображения кастомной иконки кубика (пасхалка на уникалки и случайную валюта)
_DIE_GAME = "DIE_GAME_MARKER"


def _is_die_game_reward(name: str) -> bool:
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

        # Инициализируем и подготавливаем кастомную иконку кубика
        self._init_custom_icon()

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

    def _init_custom_icon(self) -> None:
        """Загрузка, масштабирование и динамическое перекрашивание иконки die_game.png."""
        _base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        self._icon_path = os.path.join(_base, "icons", "die_game.png")
        self._has_icon = os.path.exists(self._icon_path)
        self._tinted_icon = None
        self._shadow_icon = None

        if self._has_icon:
            orig_pixmap = QPixmap(self._icon_path)
            if not orig_pixmap.isNull():
                # Размер увеличен на 50% от прошлых правок (36 пикселей)
                icon_size = 36 
                
                scaled = orig_pixmap.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                
                # 1. Создаем цветную (золотую) версию иконки
                self._tinted_icon = QPixmap(scaled.size())
                self._tinted_icon.fill(Qt.transparent)
                p = QPainter(self._tinted_icon)
                p.drawPixmap(0, 0, scaled)
                p.setCompositionMode(QPainter.CompositionMode_SourceIn)
                p.fillRect(self._tinted_icon.rect(), QColor(self._config.fg_found))
                p.end()
                
                # 2. Создаем темную теневую подложку (черный цвет с альфой 180) для читаемости
                self._shadow_icon = QPixmap(scaled.size())
                self._shadow_icon.fill(Qt.transparent)
                p_sh = QPainter(self._shadow_icon)
                p_sh.drawPixmap(0, 0, scaled)
                p_sh.setCompositionMode(QPainter.CompositionMode_SourceIn)
                p_sh.fillRect(self._shadow_icon.rect(), QColor(0, 0, 0, 180))
                p_sh.end()
            else:
                self._has_icon = False

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
            return [(0, 8, "чтение...", self._config.fg_reading)]
        items = []
        for row in rows:
            text, color = self._format_row(row)
            y = max(0, row.center_y - self._config.font_size)
            items.append((0, y, text, color))
        return items

    def _format_row(self, row: PriceRow) -> tuple[str, str]:
        if _is_die_game_reward(row.name):
            return (_DIE_GAME, self._config.fg_found)
        if not row.has_price:
            # Изменено: теперь прочерк выводится золотым цветом (self._config.fg_found)
            return (NOT_FOUND_MARKER, self._config.fg_found)
        if row.divine_value >= 1:
            return (f"{row.divine_value:.2f} div", self._config.fg_found)
        return (f"{row.exalted_value:.1f} ex", self._config.fg_found)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Фон оверлея
        if self._config.bg_alpha > 0:
            bg = QColor(0, 0, 0, self._config.bg_alpha)
            painter.fillRect(self.rect(), bg)

        font = QFont(self._config.font_family, self._config.font_size)
        font.setBold(True)
        painter.setFont(font)
        
        fm = QFontMetrics(font)
        overlay_w = self.width()

        for _, y, text, color in self._items:
            if text == _DIE_GAME and self._has_icon:
                # Центрирование иконки кубика по горизонтали
                icon_w = self._tinted_icon.width()
                center_x = (overlay_w - icon_w) // 2
                
                # Вертикальное центрирование
                icon_h = self._tinted_icon.height()
                adjusted_y = y + (self._config.font_size - icon_h) // 2 + 2
                
                # Отрисовка кубика (тень + золотой силуэт) по центру
                painter.drawPixmap(center_x + 1, adjusted_y + 1, self._shadow_icon)
                painter.drawPixmap(center_x, adjusted_y, self._tinted_icon)
            else:
                # Центрирование обычного текста по горизонтали
                text_w = fm.horizontalAdvance(text)
                center_x = (overlay_w - text_w) // 2
                
                # Отрисовка текста цен с эффектом тени по центру
                painter.setPen(QColor(0, 0, 0, 180))
                painter.drawText(center_x + 1, y + self._config.font_size + 1, text)
                painter.setPen(QColor(color))
                painter.drawText(center_x, y + self._config.font_size, text)