"""
ocr_scanner.py

Windows.Media.Ocr (WinRT) backend — заменяет tesserocr.

Время одного скана: ~30-80мс против ~900мс у Tesseract.
Нет внешних зависимостей — использует встроенный Windows OCR API.

Требования:
  pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams
  Установленный языковой пакет RU:
    Windows Settings → Time & Language → Language & region → Add a language → Русский
    (Убедитесь что стоит галочка "Basic typing" для русского)

Совместимость: Windows 10 версии 1803+ / Windows 11.

ВАЖНО — параметр tessdata_dir в конструкторе сохранён для обратной совместимости
с scan_engine.py, но игнорируется — WinRT не нуждается в файлах модели.
"""

from __future__ import annotations

import asyncio
import io
import re
import sys
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageFilter

# --- Попытка импорта WinRT ---
try:
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import (
        BitmapDecoder,
        SoftwareBitmap,
        BitmapPixelFormat,
        BitmapAlphaMode,
    )
    from winrt.windows.storage.streams import (
        InMemoryRandomAccessStream,
        DataWriter,
    )
    _WINRT_AVAILABLE = True
except ImportError:
    _WINRT_AVAILABLE = False

# --- Препроцессинг (идентично оригинальному ocr_scanner.py) ---
ICON_COLUMN_FRACTION = 0.30   # обрезаем левые 30% (иконки рун)
RIGHT_TRIM_FRACTION  = 0.02   # обрезаем правые 2% (край панели)
UPSCALE_FACTOR = 2

# --- Фильтрация строк ---
MIN_NAME_LENGTH  = 4
MIN_WORD_LENGTH  = 4
PANEL_HEADER_MARKER = "рунотворческие"

# Фиктивный confidence — WinRT не возвращает уверенность, используем 90
WINRT_CONFIDENCE = 90.0

# Y-допуск (в координатах исходного изображения) для объединения строк
MERGE_Y_TOLERANCE = 25

# Языки OCR в порядке приоритета
_OCR_LANGUAGES = ["ru-RU", "ru", "ru-RU-x-t-i0-handwrit"]


@dataclass
class OcrRow:
    raw_text:   str    # как прочитал OCR, без изменений — идёт в RuTranslator
    normalized: str    # lowercase, без пунктуации — для фильтрации
    center_y:   int    # Y-координата в координатах исходного региона (до апскейла)
    confidence: float


def normalize_text(text: str) -> str:
    """Нормализация: нижний регистр, пунктуация → пробел, схлопнуть пробелы."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _has_long_word(text: str, min_len: int) -> bool:
    """True если есть хотя бы одно слово длиной >= min_len букв подряд."""
    run = 0
    for ch in text:
        if ch.isalpha():
            run += 1
            if run >= min_len:
                return True
        else:
            run = 0
    return False


def _check_winrt() -> None:
    """Проверяет доступность WinRT и даёт понятную ошибку если нет."""
    if not _WINRT_AVAILABLE:
        raise RuntimeError(
            "\n\nWinRT OCR недоступен. Установите пакеты:\n"
            "  pip install winrt-Windows.Media.Ocr "
            "winrt-Windows.Graphics.Imaging "
            "winrt-Windows.Storage.Streams\n\n"
            "И убедитесь что в Windows установлен русский языковой пакет:\n"
            "  Settings → Time & Language → Language & region → Add a language → Русский\n"
        )


class OcrScanner:
    """
    Сканирует область экрана (PIL.Image, RGB) через Windows.Media.Ocr и возвращает
    список распознанных отфильтрованных строк.

    Публичный интерфейс идентичен оригинальному tesserocr-бэкенду:
      scanner = OcrScanner(tessdata_dir, log=..., debug=...)
      rows = scanner.scan(image)
      scanner.close()
    """

    def __init__(self, tessdata_dir: str, log: Callable | None = None, debug: bool = False):
        _check_winrt()

        self._tessdata_dir = tessdata_dir  # не используется, сохранён для совместимости
        self._log   = log or (lambda msg: None)
        self._debug = debug
        self._engine: OcrEngine | None = None

        # Создаём собственный event loop для этого потока
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._engine = self._loop.run_until_complete(self._init_engine())

    async def _init_engine(self) -> OcrEngine:
        """Инициализирует OcrEngine с русским языком."""
        # Перебираем языки в порядке приоритета
        for lang_tag in _OCR_LANGUAGES:
            try:
                lang = Language(lang_tag)
                if OcrEngine.is_language_supported(lang):
                    engine = OcrEngine.try_create_from_language(lang)
                    if engine is not None:
                        self._log(f"[WinRT OCR] движок инициализирован: {lang_tag}")
                        return engine
            except Exception:
                continue

        # Последний вариант — системный язык пользователя
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is not None:
            self._log("[WinRT OCR] движок инициализирован: системный язык")
            return engine

        raise RuntimeError(
            "Windows.Media.Ocr: не удалось создать движок для русского языка.\n"
            "Установите языковой пакет:\n"
            "  Settings → Time & Language → Language & region → Add a language → Русский"
        )

    def __enter__(self) -> "OcrScanner":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.close()

    # ------------------------------------------------------------------ #
    #  Основной метод                                                      #
    # ------------------------------------------------------------------ #

    def scan(self, region_image: Image.Image) -> list[OcrRow]:
        if self._engine is None:
            return []

        if region_image.mode != "RGB":
            region_image = region_image.convert("RGB")

        # Препроцессинг — идентично оригиналу
        w, h = region_image.size
        left_cut  = max(1, int(w * ICON_COLUMN_FRACTION))
        right_cut = int(w * RIGHT_TRIM_FRACTION)
        crop_w    = max(1, w - left_cut - right_cut)
        cropped   = region_image.crop((left_cut, 0, left_cut + crop_w, h))

        upscaled = cropped.resize(
            (max(1, cropped.width * UPSCALE_FACTOR),
             max(1, cropped.height * UPSCALE_FACTOR)),
            Image.BICUBIC,
        )
        denoised = upscaled.filter(ImageFilter.MedianFilter(size=3))

        if self._debug:
            try:
                denoised.save("debug_ocr.png")
            except Exception:
                pass

        # OCR через WinRT
        try:
            rows = self._loop.run_until_complete(
                self._run_winrt_ocr(denoised, h)
            )
        except Exception as ex:
            self._log(f"[WinRT OCR] ERROR: {ex}")
            rows = []

        return rows

    async def _run_winrt_ocr(self, image: Image.Image, region_height: int) -> list[OcrRow]:
        """Конвертирует PIL Image → SoftwareBitmap → запускает OCR."""
        bitmap = await self._pil_to_software_bitmap(image)
        ocr_result = await self._engine.recognize_async(bitmap)

        rows: list[OcrRow] = []
        diag: list[str] = []

        for line in ocr_result.lines:
            raw_text = line.text.strip() if line.text else ""

            # Y из bounding box слов линии
            center_y = self._line_center_y(line, image.height, region_height)

            reject = None
            normalized = ""

            if not raw_text:
                reject = "empty"
            else:
                normalized = normalize_text(raw_text)
                if len(normalized) < MIN_NAME_LENGTH:
                    reject = "short"
                elif not _has_long_word(normalized, MIN_WORD_LENGTH):
                    reject = "noword"
                elif PANEL_HEADER_MARKER in normalized:
                    reject = "header"

            label = f"y={center_y} conf={WINRT_CONFIDENCE:.0f} '{raw_text}'"
            if reject:
                label += f" REJ:{reject}"
            diag.append(label)

            if reject is None:
                rows.append(OcrRow(
                    raw_text=raw_text,
                    normalized=normalized,
                    center_y=center_y,
                    confidence=WINRT_CONFIDENCE,
                ))

        if diag:
            self._log("OCR raw " + str(len(diag)) + " lines -> " + " | ".join(diag))

        return rows

    @staticmethod
    def _line_center_y(line, image_height: int, region_height: int) -> int:
        """Вычисляет центр строки по bounding box слов, масштабирует в координаты региона."""
        try:
            ys = [w.bounding_rect.y for w in line.words]
            hs = [w.bounding_rect.height for w in line.words]
            if not ys:
                return 0
            top    = min(ys)
            bottom = max(y + h_ for y, h_ in zip(ys, hs))
            center = (top + bottom) / 2
            # Масштабируем из координат апскейленного изображения в координаты региона
            scaled = int(center / UPSCALE_FACTOR)
            return max(0, min(scaled, region_height - 1))
        except Exception:
            return 0

    @staticmethod
    async def _pil_to_software_bitmap(image: Image.Image) -> SoftwareBitmap:
        """Конвертирует PIL Image в SoftwareBitmap через BMP поток."""
        buf = io.BytesIO()
        image.save(buf, format="BMP")
        bmp_bytes = buf.getvalue()

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(bmp_bytes)
        await writer.store_async()
        writer.detach_stream()

        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        return await decoder.get_software_bitmap_async()


# ------------------------------------------------------------------ #
#  Самопроверка из командной строки                                    #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    import time

    if len(sys.argv) < 2:
        print("Использование: python ocr_scanner.py <screenshot.png> [x y w h]")
        sys.exit(1)

    image_path = sys.argv[1]
    img = Image.open(image_path).convert("RGB")

    if len(sys.argv) >= 6:
        x, y, ww, hh = map(int, sys.argv[2:6])
        img = img.crop((x, y, x + ww, y + hh))

    with OcrScanner("", log=print, debug=True) as scanner:
        # Прогрев
        scanner.scan(img)

        # Замер
        t0 = time.time()
        rows = scanner.scan(img)
        elapsed = (time.time() - t0) * 1000

        print(f"\nВремя скана: {elapsed:.0f} мс")
        print(f"Распознано строк: {len(rows)}")
        for r in rows:
            print(f"  y={r.center_y:4d} conf={r.confidence:5.1f} '{r.raw_text}'")
