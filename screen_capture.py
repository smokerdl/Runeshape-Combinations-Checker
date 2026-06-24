"""
screen_capture.py

Захват прямоугольной области экрана. Эквивалент ScreenCapture.cs.
"""
from __future__ import annotations

import mss
from PIL import Image


def capture_region(x: int, y: int, width: int, height: int) -> Image.Image:
    """Захватывает прямоугольную область экрана и возвращает PIL.Image (RGB).
    Координаты — абсолютные, в пикселях экрана (как возвращает инструмент
    калибровки)."""
    monitor = {"left": x, "top": y, "width": max(1, width), "height": max(1, height)}
    with mss.mss() as sct:
        shot = sct.grab(monitor)
        # mss отдаёт BGRA — конвертируем в RGB через PIL
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img
