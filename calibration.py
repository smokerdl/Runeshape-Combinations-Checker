"""
calibration.py

Выбор области захвата мышью поверх игры. Эквивалент CalibrationOverlay.cs.
Реализовано на PySide6 (Qt) — см. overlay.py для истории перехода с tkinter
(там же подробности про Qt.WindowTransparentForInput для click-through).

ОГРАНИЧЕНИЕ MVP: захватывается только ПЕРВИЧНЫЙ монитор (см. ТЗ, раздел 2 —
мультимонитор отложен на Фазу 2).

ВАЖНО: эта функция НЕ создаёт свой QApplication — на процесс должно быть ровно
одно Qt-приложение (создаётся в main.py). run_calibration() можно вызывать в
любой момент, пока существующий QApplication уже запущен.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QPoint, QEventLoop
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QMouseEvent, QKeyEvent
from PySide6.QtWidgets import QWidget, QApplication

from config import Region


class _CalibrationWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._start: QPoint | None = None
        self._end: QPoint | None = None
        self._result: Region | None = None
        self._loop = QEventLoop()

        screen = QApplication.primaryScreen()
        geo = screen.geometry()   # абсолютные координаты первичного монитора
        self.setGeometry(geo)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)

    def run(self) -> Region | None:
        self.showFullScreen()
        self.activateWindow()
        self.raise_()
        self._loop.exec()
        return self._result

    # --- Обработка мыши/клавиатуры ---

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._start = event.position().toPoint()
        self._end = self._start
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start is not None:
            self._end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._start is None:
            return
        self._end = event.position().toPoint()

        # ВАЖНО: не используем QRect.width()/height() напрямую — у QRect
        # "включительная" система координат (width = right - left + 1), из-за
        # чего размер получается на 1px больше интуитивно ожидаемого. Считаем
        # явно по разнице точек, чтобы регион точно соответствовал растянутому
        # курсором прямоугольнику.
        x = min(self._start.x(), self._end.x())
        y = min(self._start.y(), self._end.y())
        width = abs(self._end.x() - self._start.x())
        height = abs(self._end.y() - self._start.y())

        if width < 5 or height < 5:
            # Слишком маленькая область — скорее всего случайный клик, игнорируем
            self._start = None
            self._end = None
            self.update()
            return

        # geometry().topLeft() — смещение этого окна относительно (0,0) экрана;
        # на одномониторной настройке обычно (0,0), но учитываем на будущее.
        offset = self.geometry().topLeft()
        self._result = Region(
            x=x + offset.x(),
            y=y + offset.y(),
            width=width,
            height=height,
        )
        self._loop.quit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self._result = None
            self._loop.quit()

    # --- Отрисовка ---

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Полупрозрачная дымка на весь экран — видно игру под оверлеем
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))

        font = QFont("Segoe UI", 14, QFont.Bold)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(
            QRect(0, 20, self.width(), 40), Qt.AlignHCenter,
            "Выдели область с наградами зажатой левой кнопкой мыши. Esc — отмена."
        )

        if self._start is not None and self._end is not None:
            rect = QRect(self._start, self._end).normalized()
            pen = QPen(QColor("#00FF80"))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(rect)


def run_calibration() -> Region | None:
    """
    Показывает полупрозрачный оверлей на весь (первичный) экран, даёт выделить
    прямоугольник зажатой левой кнопкой мыши. Esc отменяет выбор.

    Блокирует выполнение до завершения выбора (через локальный QEventLoop, без
    вложенного app.exec()), затем возвращает Region с абсолютными координатами
    выделенной области, либо None при отмене.
    """
    widget = _CalibrationWidget()
    return widget.run()


# --- Самопроверка: показать оверлей и вывести выбранные координаты в консоль ---
# Запускать только на машине с реальным дисплеем (Windows с запущенной игрой
# для проверки визуального наложения).
if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    region = run_calibration()
    if region is not None:
        print(f"Выбрана область: x={region.x} y={region.y} width={region.width} height={region.height}")
    else:
        print("Калибровка отменена (Esc)")
