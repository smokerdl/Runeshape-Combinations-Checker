"""
settings_window.py

Оверлей в трее: при запуске разворачивается в правом нижнем углу экрана
(над панелью задач), имитируя системные флайауты Windows.
Дизайн полностью адаптирован под графический стиль Path of Exile 2.
Стандартное окно ОС скрыто, перетаскивание отключено для интеграции с треем.
"""
from __future__ import annotations

import os
import sys
import math

from PySide6.QtCore import Qt, Signal, QObject, QThread, QTimer, QRect, QPoint
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QKeySequence, QFont, QPen, QBrush,
    QLinearGradient, QPainterPath
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QSystemTrayIcon, QMenu, QApplication, QKeySequenceEdit, QFrame
)

from pynput import keyboard as kb

# Предполагается, что эти модули существуют в вашем проекте
import config as config_module
from config import AppConfig, AVAILABLE_LEAGUES, DEFAULT_HOTKEY
from calibration import run_calibration
from price_repository import PriceRepository
from scan_engine import ScanEngine, PriceRow
from overlay import PriceOverlay

TESSDATA_DIR = "tessdata"
JSON_PATH = "Runeshape_Combinations.json"
LOG_PATH = "scan_log.txt"

# --- Константы стилей PoE2 ---
POE_BG_COLOR = QColor("#121212")
POE_FRAME_COLOR = QColor("#8B6914")
POE_TEXT_GOLD = QColor("#FFD700")
POE_TEXT_GREY = QColor("#A0A0A0")
POE_BTN_BG_TOP = QColor("#4D4D4D")
POE_BTN_BG_BOTTOM = QColor("#2D2D2D")

# Шрифты
POE_FONT_SERIF = "Georgia"
POE_FONT_SANS = "Verdana"


def _make_tray_icon() -> QIcon:
    """Загрузка официальной иконки логотипа приложения по абсолютному пути."""
    base_dir = os.path.dirname(os.path.abspath(__file__))

    for name in ["logo.png", "logo.webp", "logo.ico", "Logo.png", "Logo.webp", "Logo.ico"]:
        for folder in ["icons", ""]:
            path = os.path.join(base_dir, folder, name)
            if os.path.exists(path):
                icon = QIcon(path)
                if not icon.isNull():
                    return icon

    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)

    painter.setBrush(QColor("#8B6914"))
    painter.setPen(QColor("#FFD700"))
    painter.drawEllipse(1, 1, 30, 30)

    painter.setPen(QPen(QColor("#B8860B"), 1))
    painter.drawEllipse(3, 3, 26, 26)

    painter.setPen(QColor("#FFD700"))
    font_rune = QFont(POE_FONT_SANS, 7)
    painter.setFont(font_rune)
    runes = ["ᚱ", "ᚢ", "ᚾ", "", "ᛋ", "ᚼ", "ᚷ", "ᛚ"]
    for i in range(8):
        angle = i * 45
        x = 16 + 11 * math.cos(math.radians(angle - 90))
        y = 16 + 11 * math.sin(math.radians(angle - 90))
        painter.drawText(QPoint(int(x - 3), int(y + 3)), runes[i])

    painter.setPen(QColor("#FFD700"))
    font_r = QFont(POE_FONT_SERIF, 16, QFont.Bold)
    painter.setFont(font_r)
    painter.drawText(pix.rect(), Qt.AlignCenter, "R")
    painter.end()
    return QIcon(pix)


class _Bridge(QObject):
    state_changed = Signal(list, bool, bool)
    prices_updated = Signal()
    hotkey_pressed = Signal()
    hotkey_captured = Signal(str)


class _PriceFetchWorker(QThread):
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, repo: PriceRepository, league_name: str):
        super().__init__()
        self._repo = repo
        self._league_name = league_name

    def run(self) -> None:
        try:
            self._repo.initial_fetch(self._league_name)
            self.finished_ok.emit()
        except Exception as ex:
            self.failed.emit(str(ex))


class PoeLabel(QLabel):
    def __init__(self, text="", parent=None, is_title=False, is_secondary=False):
        super().__init__(text, parent)
        font_family = POE_FONT_SERIF if is_title or not is_secondary else POE_FONT_SANS
        font_size = 11 if is_title else (9 if is_secondary else 10)
        self.setFont(QFont(font_family, font_size))

        if is_secondary:
            self.setStyleSheet(f"color: {POE_TEXT_GREY.name()};")
        else:
            self.setStyleSheet(f"color: {POE_TEXT_GOLD.name()};")
        self.setWordWrap(True)


class PoeComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(False)
        self.setFont(QFont(POE_FONT_SERIF, 10))

        self.setStyleSheet(f"""
            QComboBox {{
                color: {POE_TEXT_GOLD.name()};
                background-color: {POE_BG_COLOR.name()};
                border: 2px solid {POE_FRAME_COLOR.name()};
                border-radius: 2px;
                padding: 3px 10px 3px 5px;
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border-left: 1px solid {POE_FRAME_COLOR.name()};
            }}
            QComboBox::down-arrow {{
                image: none;
            }}
            QComboBox QAbstractItemView {{
                background-color: {POE_BG_COLOR.name()};
                color: {POE_TEXT_GOLD.name()};
                selection-background-color: {POE_FRAME_COLOR.name()};
                selection-color: {POE_BG_COLOR.name()};
                border: 2px solid {POE_FRAME_COLOR.name()};
                outline: 0px;
            }}
        """)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        arrow_width = 8
        arrow_height = 6
        rect = self.rect()
        x = rect.width() - 14
        y = rect.height() // 2 - arrow_height // 2 + 1

        painter.setPen(Qt.NoPen)
        painter.setBrush(POE_FRAME_COLOR)
        path = QPainterPath()
        path.moveTo(x, y)
        path.lineTo(x + arrow_width, y)
        path.lineTo(x + arrow_width // 2, y + arrow_height)
        path.closeSubpath()
        painter.drawPath(path)


class PoeButton(QPushButton):
    def __init__(self, text="", parent=None, is_primary=False):
        super().__init__(text, parent)
        self.setFont(QFont(POE_FONT_SERIF, 10, QFont.Bold))
        self._is_primary = is_primary
        self.setMinimumHeight(28)
        self.setStyleSheet(f"""
            QPushButton {{
                color: {POE_TEXT_GOLD.name()};
                border: none;
                background: transparent;
            }}
            QPushButton:pressed {{
                color: {POE_TEXT_GREY.name()};
            }}
        """)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        gradient = QLinearGradient(0, 0, 0, rect.height())
        if self._is_primary:
            gradient.setColorAt(0, POE_BG_COLOR.lighter(150))
            gradient.setColorAt(1, POE_BG_COLOR)
        else:
            gradient.setColorAt(0, POE_BTN_BG_TOP)
            gradient.setColorAt(1, POE_BTN_BG_BOTTOM)

        painter.setBrush(gradient)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 2, 2)

        painter.setPen(QPen(POE_FRAME_COLOR, 1.5))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 2, 2)

        painter.setPen(QPen(POE_FRAME_COLOR, 1))
        c = 5
        painter.drawLine(0, c, 0, 0)
        painter.drawLine(0, 0, c, 0)
        painter.drawLine(rect.width() - c, 0, rect.width(), 0)
        painter.drawLine(rect.width(), 0, rect.width(), c)
        painter.drawLine(rect.width(), rect.height() - c, rect.width(), rect.height())
        painter.drawLine(rect.width(), rect.height(), rect.width() - c, rect.height())
        painter.drawLine(c, rect.height(), 0, rect.height())
        painter.drawLine(0, rect.height(), 0, rect.height() - c)

        super().paintEvent(event)


class PoePrimaryButton(PoeButton):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent, is_primary=True)


class PoeSecondaryButton(PoeButton):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent, is_primary=False)


class PoeMinimizeButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setCursor(Qt.ArrowCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        bg = QColor("#332A15") if self.underMouse() else QColor("#1A150B")
        painter.setBrush(bg)
        painter.setPen(QPen(POE_FRAME_COLOR, 1.5))
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))

        painter.setPen(QPen(POE_TEXT_GOLD, 2))
        painter.drawLine(6, 10, 14, 10)


class PoeCloseButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setCursor(Qt.ArrowCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()

        bg = QColor("#661111") if self.underMouse() else QColor("#3D0A0A")
        painter.setBrush(bg)
        painter.setPen(QPen(POE_FRAME_COLOR, 1.5))
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))

        painter.setPen(QPen(POE_TEXT_GOLD, 2))
        painter.drawLine(6, 6, 14, 14)
        painter.drawLine(14, 6, 6, 14)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Runeshape Combinations Checker")
        self.setFixedWidth(400)

        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setStyleSheet(f"background-color: {POE_BG_COLOR.name()};")

        self._config: AppConfig = AppConfig()
        self._repo: PriceRepository | None = None
        self._engine: ScanEngine | None = None
        self._overlay: PriceOverlay | None = None
        self._fetch_worker: _PriceFetchWorker | None = None
        self._hotkey_listener = None
        self._rebinding = False
        self._restart_after_league_change = False

        self._bridge = _Bridge()
        self._bridge.prices_updated.connect(self._update_status_label)
        self._bridge.hotkey_pressed.connect(self._on_start_stop)
        self._bridge.hotkey_captured.connect(self._on_hotkey_captured)

        self._build_ui()
        self._build_tray()
        self._load_config_and_start()
        self._start_hotkey_listener()

        self._position_at_bottom_right()

    def _position_at_bottom_right(self) -> None:
        self.adjustSize()
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            margin = 12
            bottom_margin = 60

            x = geom.right() - self.width() - margin
            y = geom.bottom() - self.height() - bottom_margin
            self.move(x, y)

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.setPen(QPen(POE_FRAME_COLOR, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(15, 12, 15, 15)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 5)
        title_row.addSpacing(46)

        title_label = PoeLabel("RUNESHAPE COMBINATIONS CHECKER", is_title=True)
        title_label.setAlignment(Qt.AlignCenter)
        title_row.addWidget(title_label, 1)

        self.minimize_button = PoeMinimizeButton()
        self.minimize_button.clicked.connect(self.showMinimized)
        title_row.addWidget(self.minimize_button)

        self.close_button = PoeCloseButton()
        self.close_button.clicked.connect(self.close)
        title_row.addWidget(self.close_button)

        layout.addLayout(title_row)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"background-color: {POE_FRAME_COLOR.name()}; max-height: 1px; border: none;")
        layout.addWidget(line)

        league_row = QHBoxLayout()
        league_row.addWidget(PoeLabel("Лига:"))
        self.league_box = PoeComboBox()
        self.league_box.addItems(AVAILABLE_LEAGUES)
        league_row.addWidget(self.league_box)
        layout.addLayout(league_row)

        self.region_label = PoeLabel("Не откалибровано", is_secondary=True)
        self.region_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.region_label)

        self.calibrate_button = PoeSecondaryButton("Калибровать область")
        self.calibrate_button.clicked.connect(self._on_calibrate)
        layout.addWidget(self.calibrate_button)

        self.status_label = PoeLabel("Ожидание — цены не загружены", is_secondary=True)
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        self.start_stop_button = PoePrimaryButton("Старт")
        self.start_stop_button.setEnabled(False)
        self.start_stop_button.clicked.connect(self._on_start_stop)
        layout.addWidget(self.start_stop_button)

        hotkey_row = QHBoxLayout()
        hotkey_row.addWidget(PoeLabel("Хоткей старт/стоп:"))
        self.hotkey_label = PoeLabel(DEFAULT_HOTKEY.upper(), is_secondary=True)
        self.hotkey_label.setFont(QFont(POE_FONT_SANS, 10, QFont.Bold))
        hotkey_row.addWidget(self.hotkey_label)
        hotkey_row.addStretch()

        self.rebind_button = PoeSecondaryButton("Изменить")
        self.rebind_button.setFixedWidth(80)
        self.rebind_button.clicked.connect(self._on_rebind_click)
        hotkey_row.addWidget(self.rebind_button)
        layout.addLayout(hotkey_row)

    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_make_tray_icon())
        self._tray.setToolTip("Runeshape Combinations Checker")

        menu = QMenu()
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {POE_BG_COLOR.name()};
                color: {POE_TEXT_GOLD.name()};
                border: 1px solid {POE_FRAME_COLOR.name()};
            }}
            QMenu::item:selected {{
                background-color: {POE_FRAME_COLOR.name()};
                color: {POE_BG_COLOR.name()};
            }}
        """)
        show_action = menu.addAction("Открыть")
        show_action.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        quit_action = menu.addAction("Выход")
        quit_action.triggered.connect(self._quit_app)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._show_from_tray()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self._position_at_bottom_right()

        if self.isMinimized():
            self.setWindowState(Qt.WindowNoState)

        self.show()
        self.raise_()
        self.activateWindow()

    def _load_config_and_start(self) -> None:
        self._config = config_module.load()
        idx = self.league_box.findText(self._config.league_name)
        self.league_box.setCurrentIndex(idx if idx >= 0 else 0)
        self._update_region_label()
        self._update_hotkey_label()
        self._start_price_fetch()
        self.league_box.currentIndexChanged.connect(self._on_league_changed)

    def _update_region_label(self) -> None:
        if self._config.is_calibrated:
            r = self._config.region
            self.region_label.setText(f"x={r.x} y={r.y} {r.width}×{r.height}")
        else:
            self.region_label.setText("Не откалибровано")
        self._refresh_start_button_enabled()
        self._position_at_bottom_right()

    def _update_hotkey_label(self) -> None:
        self.hotkey_label.setText(self._config.start_stop_hotkey.upper())

    def _refresh_start_button_enabled(self) -> None:
        ready = (
            self._config.is_calibrated
            and self._repo is not None
            and self._repo.item_count > 0
        )
        if self._engine is None:
            self.start_stop_button.setEnabled(ready)

    def _start_price_fetch(self) -> None:
        self.status_label.setText("Загрузка цен с poe.ninja…")
        self.start_stop_button.setEnabled(False)
        self.league_box.setEnabled(False)
        if self._repo is not None:
            self._repo.stop_auto_refresh()
        self._repo = PriceRepository(log=print)
        self._repo.on_prices_updated(lambda: self._bridge.prices_updated.emit())
        self._fetch_worker = _PriceFetchWorker(self._repo, self._config.league_name)
        self._fetch_worker.finished_ok.connect(self._on_fetch_done)
        self._fetch_worker.failed.connect(self._on_fetch_failed)
        self._fetch_worker.start()

    def _on_fetch_done(self) -> None:
        self.league_box.setEnabled(True)
        self._repo.start_auto_refresh(self._config.league_name)
        self._update_status_label()

        if self._restart_after_league_change:
            self._restart_after_league_change = False
            if self._start_engine():
                self.start_stop_button.setText("Стоп")
                self.hide()
                return

        self._refresh_start_button_enabled()

    def _on_fetch_failed(self, msg: str) -> None:
        self.league_box.setEnabled(True)
        self._restart_after_league_change = False
        self.status_label.setText(f"Ошибка загрузки цен: {msg}")
        self._position_at_bottom_right()

    def _update_status_label(self) -> None:
        if self._repo is None:
            return
        fetched = self._repo.last_fetched_at
        fetched_str = fetched.strftime("%d.%m %H:%M") if fetched else "никогда"
        self.status_label.setText(
            f"{self._repo.item_count} позиции загружено  ·  обновлено {fetched_str}"
        )
        self._refresh_start_button_enabled()
        self._position_at_bottom_right()

    def _on_league_changed(self, idx: int) -> None:
        text = self.league_box.itemText(idx)
        if not text or text == self._config.league_name:
            return

        # Если сканирование уже активно, запоминаем это состояние и полностью
        # останавливаем старый ScanEngine/overlay перед загрузкой новой лиги.
        self._restart_after_league_change = self._engine is not None
        if self._restart_after_league_change:
            self._stop_engine()

        self._config.league_name = text
        config_module.save(self._config)
        self._start_price_fetch()

    def _on_calibrate(self) -> None:
        region = run_calibration()
        if region is None:
            return
        self._config.region = region
        config_module.save(self._config)
        self._update_region_label()

    def _start_engine(self) -> bool:
        if not self._config.is_calibrated or self._repo is None or self._repo.item_count == 0:
            return False

        self._overlay = PriceOverlay(self._config.region.as_tuple())
        self._bridge.state_changed.connect(self._overlay.update_state)
        self._engine = ScanEngine(
            region=self._config.region.as_tuple(),
            tessdata_dir=TESSDATA_DIR,
            json_path=JSON_PATH,
            price_repo=self._repo,
            log_path=LOG_PATH,
            on_state=self._on_engine_state,
            debug=True,
        )
        self._engine.start()
        return True

    def _stop_engine(self) -> None:
        if self._engine is not None:
            self._engine.stop_and_wait()
            self._engine = None

        if self._overlay is not None:
            try:
                self._bridge.state_changed.disconnect(self._overlay.update_state)
            except (RuntimeError, TypeError):
                pass
            self._overlay.hide()
            self._overlay = None

        self.start_stop_button.setText("Старт")

    def _on_start_stop(self) -> None:
        if self._engine is None:
            if self._start_engine():
                self.start_stop_button.setText("Стоп")
                self.hide()
        else:
            self._stop_engine()

    def _on_engine_state(self, rows: list[PriceRow], confirmed: bool, reading: bool) -> None:
        self._bridge.state_changed.emit(rows, confirmed, reading)

    def _start_hotkey_listener(self) -> None:
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        key = self._config.start_stop_hotkey
        key_combo = key if len(key) == 1 else f"<{key}>"
        try:
            self._hotkey_listener = kb.GlobalHotKeys({
                key_combo: lambda: self._bridge.hotkey_pressed.emit()
            })
            self._hotkey_listener.start()
        except Exception as ex:
            print(f"[hotkey] не удалось зарегистрировать хоткей '{key}': {ex}")

    def _on_rebind_click(self) -> None:
        if self._rebinding:
            return
        self._rebinding = True
        self.rebind_button.setEnabled(False)
        self.rebind_button.setText("...")
        self.hotkey_label.setText("Нажмите клавишу")

        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass

        _VK_ALPHA = {vk: chr(vk).lower() for vk in range(65, 91)}
        _VK_DIGIT = {vk: chr(vk) for vk in range(48, 58)}
        _VK_TO_EN = {**_VK_ALPHA, **_VK_DIGIT}

        def on_press(key):
            try:
                vk = getattr(key, 'vk', None)
                if vk is not None and vk in _VK_TO_EN:
                    key_name = _VK_TO_EN[vk]
                elif hasattr(key, 'name') and key.name:
                    key_name = key.name.lower()
                elif hasattr(key, 'char') and key.char and key.char.isprintable():
                    key_name = key.char.lower()
                else:
                    key_name = str(key).replace("Key.", "").lower()
            except Exception:
                key_name = str(key).replace("Key.", "").lower()

            if key_name:
                self._bridge.hotkey_captured.emit(key_name)

            return False

        listener = kb.Listener(on_press=on_press)
        listener.start()

    def _on_hotkey_captured(self, new_key: str) -> None:
        if new_key not in ("esc", "escape"):
            self._config.start_stop_hotkey = new_key
            config_module.save(self._config)

        self._update_hotkey_label()
        self._start_hotkey_listener()

        self._rebinding = False
        self.rebind_button.setEnabled(True)
        self.rebind_button.setText("Изменить")

    def changeEvent(self, event) -> None:
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.WindowStateChange:
            if self.isMinimized():
                QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        self._quit_app()
        event.accept()

    def _quit_app(self) -> None:
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        self._stop_engine()
        if self._repo is not None:
            self._repo.stop_auto_refresh()
        if self._overlay is not None:
            self._overlay.hide()
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()
