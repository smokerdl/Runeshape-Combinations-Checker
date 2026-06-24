"""
settings_window.py

Оверлей в трее: при запуске разворачивается, при нажатии "-" или Старт
сворачивается в трей. Нажатие на иконку трея разворачивает обратно.
Дизайн полностью адаптирован под графический стиль Path of Exile 2.
Стандартное окно ОС скрыто, реализована кастомная игровая панель управления.
"""
from __future__ import annotations

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
POE_BG_COLOR = QColor("#121212")        # Почти черный фон игры
POE_FRAME_COLOR = QColor("#8B6914")     # Бронза для рамок и акцентов
POE_TEXT_GOLD = QColor("#FFD700")       # Золотой текст для заголовков
POE_TEXT_GREY = QColor("#A0A0A0")       # Серый текст для второстепенной информации
POE_BTN_BG_TOP = QColor("#4D4D4D")
POE_BTN_BG_BOTTOM = QColor("#2D2D2D")

# Шрифты
POE_FONT_SERIF = "Georgia"
POE_FONT_SANS = "Verdana"


def _make_tray_icon() -> QIcon:
    """Детализированная иконка для трея — золотая монета с буквами и рунами."""
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)

    # Золотая монета (фон)
    painter.setBrush(QColor("#8B6914"))
    painter.setPen(QColor("#FFD700"))
    painter.drawEllipse(1, 1, 30, 30)

    # Декоративный край
    painter.setPen(QPen(QColor("#B8860B"), 1))
    painter.drawEllipse(3, 3, 26, 26)

    # Руны вокруг
    painter.setPen(QColor("#FFD700"))
    font_rune = QFont(POE_FONT_SANS, 7)
    painter.setFont(font_rune)
    runes = ["ᚱ", "ᚢ", "ᚾ", "ᛖ", "ᛋ", "ᚼ", "ᚷ", "ᛚ"]
    for i in range(8):
        angle = i * 45
        x = 16 + 11 * math.cos(math.radians(angle - 90))
        y = 16 + 11 * math.sin(math.radians(angle - 90))
        painter.drawText(QPoint(int(x - 3), int(y + 3)), runes[i])

    # Буква "R" по центру
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


# --- Кастомные виджеты в стиле PoE2 ---

class PoeLabel(QLabel):
    """Лейбл с serif шрифтом и золотым/серым цветом текста."""
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
    """Выпадающий список со сложной бронзовой рамкой и исправленным отображением элементов."""
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
        """Рисуем кастомную стрелочку-треугольник."""
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
    """Базовый класс для кнопок со сложной рамкой и градиентом."""
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

        # Угловые декоративные элементы
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


# --- Управляющие кнопки для кастомной шапки окна ---

class PoeMinimizeButton(QPushButton):
    """Кастомный круглый минус для сворачивания."""
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
    """Кастомный мрачно-красный крестик закрытия из PoE2."""
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


# --- Главное окно ---

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Runeshape Combinations Checker")
        self.setFixedWidth(400)
        
        # Полностью отключаем системное оформление Windows (Frameless)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setStyleSheet(f"background-color: {POE_BG_COLOR.name()};")

        self._config: AppConfig = AppConfig()
        self._repo: PriceRepository | None = None
        self._engine: ScanEngine | None = None
        self._overlay: PriceOverlay | None = None
        self._fetch_worker: _PriceFetchWorker | None = None
        self._hotkey_listener = None
        self._rebinding = False
        self._drag_pos = None  # Для перемещения окна

        self._bridge = _Bridge()
        self._bridge.prices_updated.connect(self._update_status_label)
        self._bridge.hotkey_pressed.connect(self._on_start_stop)

        self._build_ui()
        self._build_tray()
        self._load_config_and_start()
        self._start_hotkey_listener()

    # --- Поддержка перетаскивания окна мышью ---

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if event.position().y() < 40:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()

    # --- Отрисовка красивой общей рамки вокруг окна ---

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.setPen(QPen(POE_FRAME_COLOR, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))

    # --- Построение UI ---

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(15, 12, 15, 15)

        # Кастомная верхняя панель (Шапка) вместо системной
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

        # Лига
        league_row = QHBoxLayout()
        league_row.addWidget(PoeLabel("Лига:"))
        self.league_box = PoeComboBox()
        self.league_box.addItems(AVAILABLE_LEAGUES)
        league_row.addWidget(self.league_box)
        layout.addLayout(league_row)

        # Координаты
        self.region_label = PoeLabel("Не откалибровано", is_secondary=True)
        self.region_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.region_label)

        # Кнопка калибровки
        self.calibrate_button = PoeSecondaryButton("Калибровать область")
        self.calibrate_button.clicked.connect(self._on_calibrate)
        layout.addWidget(self.calibrate_button)

        # Статус
        self.status_label = PoeLabel("Ожидание — цены не загружены", is_secondary=True)
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        # Кнопка Старт/Стоп
        self.start_stop_button = PoePrimaryButton("Старт")
        self.start_stop_button.setEnabled(False)
        self.start_stop_button.clicked.connect(self._on_start_stop)
        layout.addWidget(self.start_stop_button)

        # Хоткей
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

    # --- Трей ---

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
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    # --- Конфигурация и загрузка цен ---

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
        if self._repo is not None:
            self._repo.stop_auto_refresh()
        self._repo = PriceRepository(log=print)
        self._repo.on_prices_updated(lambda: self._bridge.prices_updated.emit())
        self._fetch_worker = _PriceFetchWorker(self._repo, self._config.league_name)
        self._fetch_worker.finished_ok.connect(self._on_fetch_done)
        self._fetch_worker.failed.connect(self._on_fetch_failed)
        self._fetch_worker.start()

    def _on_fetch_done(self) -> None:
        self._repo.start_auto_refresh(self._config.league_name)
        self._update_status_label()
        self._refresh_start_button_enabled()

    def _on_fetch_failed(self, msg: str) -> None:
        self.status_label.setText(f"Ошибка загрузки цен: {msg}")

    def _update_status_label(self) -> None:
        if self._repo is None:
            return
        fetched = self._repo.last_fetched_at
        fetched_str = fetched.strftime("%d.%m %H:%M") if fetched else "никогда"
        self.status_label.setText(
            f"{self._repo.item_count} позиций загружено  ·  обновлено {fetched_str}"
        )
        self._refresh_start_button_enabled()

    def _on_league_changed(self, idx: int) -> None:
        text = self.league_box.itemText(idx)
        if not text or text == self._config.league_name:
            return
        self._config.league_name = text
        config_module.save(self._config)
        self._start_price_fetch()

    # --- Калибровка ---

    def _on_calibrate(self) -> None:
        region = run_calibration()
        if region is None:
            return
        self._config.region = region
        config_module.save(self._config)
        self._update_region_label()

    # --- Старт/Стоп ---

    def _on_start_stop(self) -> None:
        if self._engine is None:
            if not self._config.is_calibrated or self._repo is None or self._repo.item_count == 0:
                return
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
            self.start_stop_button.setText("Стоп")
            self.hide()
        else:
            self._engine.stop_and_wait()
            self._engine = None
            if self._overlay is not None:
                self._overlay.hide()
                self._overlay = None
            self.start_stop_button.setText("Старт")
            self._show_from_tray()

    def _on_engine_state(self, rows: list[PriceRow], confirmed: bool, reading: bool) -> None:
        self._bridge.state_changed.emit(rows, confirmed, reading)

    # --- Хоткей ---

    def _start_hotkey_listener(self) -> None:
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        key = self._config.start_stop_hotkey
        key_combo = f"<{key}>"
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

        captured = []

        def on_press(key):
            try:
                key_name = key.char if hasattr(key, 'char') and key.char else key.name
            except Exception:
                key_name = str(key).replace("Key.", "")
            if key_name and key_name not in ("esc", "escape"):
                captured.append(key_name)
                try:
                    self._hotkey_listener.stop()
                except Exception:
                    pass
                listener.stop()

        def on_release(key):
            pass

        def finish():
            if captured:
                new_key = captured[0]
                self._config.start_stop_hotkey = new_key
                config_module.save(self._config)
                self._update_hotkey_label()
                self._start_hotkey_listener()
            else:
                self._update_hotkey_label()
                self._start_hotkey_listener()
            self._rebinding = False
            self.rebind_button.setEnabled(True)
            self.rebind_button.setText("Изменить")

        listener = kb.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        QTimer.singleShot(300, finish)

    # --- Завершение работы ---

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
        if self._engine is not None:
            self._engine.stop_and_wait()
        if self._repo is not None:
            self._repo.stop_auto_refresh()
        if self._overlay is not None:
            self._overlay.hide()
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())