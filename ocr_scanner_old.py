"""
ocr_scanner.py

Распознавание текста панели "Рунотворческие комбинации" на русском клиенте PoE2.
Эквивалент OcrScanner.cs из C#-версии.

ВАЖНО — выбор библиотеки: используется `tesserocr`, а не `pytesseract`.
`pytesseract` запускает `tesseract.exe` отдельным процессом на КАЖДЫЙ вызов и
перезагружает языковую модель с диска каждый раз — на модели "best" (20МБ) это
давало ~2000мс на один проход, что полностью несовместимо с циклом сканирования.
`tesserocr` держит движок в памяти постоянно (как `TesseractEngine` в C#-версии),
что снижает время одного прохода до ~450-650мс.

ВАЖНО — выбор модели: используется `rus.traineddata` из репозитория
`tessdata_fast` (https://github.com/tesseract-ocr/tessdata_fast), а НЕ стандартный
пакет "best", который ставится по умолчанию через apt/большинство инсталляторов.
Разница в размере ~5x (3.8МБ против 20МБ), в скорости ~25-30%, при ЭТОМ без
заметной потери точности на тестовых скриншотах панели Рунотворчества.
Файл нужно скачать отдельно и положить в `tessdata/rus.traineddata` проекта:
    https://github.com/tesseract-ocr/tessdata_fast/raw/main/rus.traineddata

ВАЖНО — частота сканирования: даже с этими оптимизациями один полный скан
(два прохода PSM 4 + PSM 11, выполняются ПОСЛЕДОВАТЕЛЬНО — параллелизм через
threading не работает из-за GIL, см. ТЗ) занимает ~900мс-1с. Поэтому цикл
сканирования в scan_engine.py использует интервал ~1 секунда, а не 100-150мс,
как в C#-версии.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image, ImageFilter
from tesserocr import PyTessBaseAPI, PSM, OEM, RIL, iterate_level


# --- Препроцессинг (см. ТЗ 5.1) ---
ICON_COLUMN_FRACTION = 0.30   # обрезаем левые 30% ширины (иконки рун)
RIGHT_TRIM_FRACTION = 0.02    # обрезаем правые 2% ширины (край панели)
UPSCALE_FACTOR = 2

# --- Фильтрация строк (см. ТЗ 5.4) ---
MIN_CONFIDENCE = 10.0
MIN_NAME_LENGTH = 4
MIN_WORD_LENGTH = 4
PANEL_HEADER_MARKER = "рунотворческие"

# --- Tesseract ---
LANG = "rus"
PSM_SINGLE_COLUMN = PSM.SINGLE_COLUMN   # основной проход — чистые списки
PSM_SPARSE_TEXT = PSM.SPARSE_TEXT       # резервный проход — спасает панели с рамками-разделителями,
                                          # где PSM 4 теряет часть строк (подтверждено тестами:
                                          # на capture_test_4.png PSM4 нашёл 2 из 4 наград, PSM11 — все 4)

# Y-допуск (в координатах исходного, НЕ апскейленного изображения) для объединения
# строк двух проходов, относящихся к одной и той же позиции на экране.
MERGE_Y_TOLERANCE = 25


@dataclass
class OcrRow:
    raw_text: str      # как прочитал Tesseract, без изменений — идёт в RuTranslator
    normalized: str     # lowercase, без пунктуации, схлопнутые пробелы — для фильтрации
    center_y: int        # Y-координата в координатах исходного региона (до апскейла)
    confidence: float


def normalize_text(text: str) -> str:
    """Нормализация, единая для всех модулей проекта:
    нижний регистр, пунктуация -> пробел, схлопнуть повторные пробелы.
    re.UNICODE гарантирует, что \\w матчит кириллицу так же, как в C#-версии."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _has_long_word(text: str, min_len: int) -> bool:
    """True, если есть хотя бы одно слово длиной >= min_len букв подряд."""
    run = 0
    for ch in text:
        if ch.isalpha():
            run += 1
            if run >= min_len:
                return True
        else:
            run = 0
    return False


class OcrScanner:
    """
    Сканирует область экрана (PIL.Image, RGB) и возвращает список распознанных,
    отфильтрованных строк. Эквивалент OcrScanner.cs.

    Движки создаются ОДИН раз в конструкторе и живут в памяти всё время работы
    программы (как TesseractEngine в C#-версии) — это и есть основной источник
    ускорения по сравнению с pytesseract. Обязательно вызвать close() при
    завершении работы (или использовать как context manager).
    """

    def __init__(self, tessdata_dir: str, log=None, debug: bool = False):
        self._tessdata_dir = tessdata_dir
        self._log = log or (lambda msg: None)
        self._debug = debug
        self._api_col = PyTessBaseAPI(path=tessdata_dir, lang=LANG, psm=PSM_SINGLE_COLUMN, oem=OEM.DEFAULT)
        self._api_sparse = PyTessBaseAPI(path=tessdata_dir, lang=LANG, psm=PSM_SPARSE_TEXT, oem=OEM.DEFAULT)

    def __enter__(self) -> "OcrScanner":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._api_col.End()
        except Exception:
            pass
        try:
            self._api_sparse.End()
        except Exception:
            pass

    def scan(self, region_image: Image.Image) -> list[OcrRow]:
        if region_image.mode != "RGB":
            region_image = region_image.convert("RGB")

        w, h = region_image.size
        left_cut = max(1, int(w * ICON_COLUMN_FRACTION))
        right_cut = int(w * RIGHT_TRIM_FRACTION)
        crop_w = max(1, w - left_cut - right_cut)
        cropped = region_image.crop((left_cut, 0, left_cut + crop_w, h))

        # Без инверсии — панель Рунотворчества: тёмный текст на светлом пергаменте.
        upscaled = cropped.resize(
            (max(1, cropped.width * UPSCALE_FACTOR), max(1, cropped.height * UPSCALE_FACTOR)),
            Image.BICUBIC,
        )

        # Медианный фильтр 3x3 (denoise) — подтверждён через OCR Tuner; также ускоряет
        # распознавание (меньше шума -> движку проще сегментировать), не только чистит текст.
        denoised = upscaled.filter(ImageFilter.MedianFilter(size=3))

        # Последовательно — параллелизм через threading не даёт выигрыша (GIL
        # не освобождается нативным кодом tesserocr в этой связке), см. ТЗ.
        rows_col = self._run_pass(self._api_col, denoised, PSM_SINGLE_COLUMN, h)
        rows_sparse = self._run_pass(self._api_sparse, denoised, PSM_SPARSE_TEXT, h)
        merged = self._merge_by_position(rows_col, rows_sparse)

        if self._debug and len(merged) <= 2:
            try:
                denoised.save("debug_ocr.png")
            except Exception:
                pass

        return merged

    def _run_pass(self, api: PyTessBaseAPI, image: Image.Image, psm, region_height: int) -> list[OcrRow]:
        api.SetImage(image)
        api.Recognize()
        ri = api.GetIterator()

        rows: list[OcrRow] = []
        diag: list[str] = []

        for r in iterate_level(ri, RIL.TEXTLINE):
            try:
                raw_text = r.GetUTF8Text(RIL.TEXTLINE) or ""
                conf = r.Confidence(RIL.TEXTLINE)
                box = r.BoundingBox(RIL.TEXTLINE)
            except RuntimeError:
                continue

            raw_text = raw_text.strip()
            if box is None:
                continue
            x1, y1, x2, y2 = box
            # Координаты считаны с апскейленного изображения — делим обратно на UPSCALE_FACTOR
            center_y = ((y1 + y2) // 2) // UPSCALE_FACTOR
            center_y = max(0, min(center_y, region_height - 1))

            reject = None
            normalized = ""
            if not raw_text:
                reject = "empty"
            elif conf < MIN_CONFIDENCE:
                reject = "lowconf"
            else:
                normalized = normalize_text(raw_text)
                if len(normalized) < MIN_NAME_LENGTH:
                    reject = "short"
                elif not _has_long_word(normalized, MIN_WORD_LENGTH):
                    reject = "noword"
                elif PANEL_HEADER_MARKER in normalized:
                    reject = "header"

            if reject is None:
                rows.append(OcrRow(raw_text=raw_text, normalized=normalized, center_y=center_y, confidence=conf))
            diag.append(f"y={center_y} conf={conf:.0f} '{raw_text}'" + (f" REJ:{reject}" if reject else ""))

        if len(rows) <= 2 and diag:
            self._log("OCR raw " + str(len(diag)) + " lines -> " + " | ".join(diag))

        return rows

    @staticmethod
    def _merge_by_position(a: list[OcrRow], b: list[OcrRow]) -> list[OcrRow]:
        def letters(s: str) -> int:
            return sum(1 for ch in s if ch.isalpha())

        result = list(a)
        for rb in b:
            idx = -1
            best_d = None
            for i, ra in enumerate(result):
                d = abs(ra.center_y - rb.center_y)
                if d <= MERGE_Y_TOLERANCE and (best_d is None or d < best_d):
                    best_d = d
                    idx = i
            if idx < 0:
                result.append(rb)
            elif letters(rb.normalized) > letters(result[idx].normalized):
                result[idx] = rb

        result.sort(key=lambda r: r.center_y)
        return result


# --- Самопроверка из командной строки: тест на готовом скриншоте без остального пайплайна ---
if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) < 3:
        print("Использование: python ocr_scanner.py <tessdata_dir> <screenshot.png> [x y w h]")
        print("  Без x y w h — берёт всё изображение целиком.")
        sys.exit(1)

    tessdata_dir = sys.argv[1]
    image_path = sys.argv[2]

    img = Image.open(image_path).convert("RGB")
    if len(sys.argv) >= 7:
        x, y, ww, hh = map(int, sys.argv[3:7])
        img = img.crop((x, y, x + ww, y + hh))

    with OcrScanner(tessdata_dir, log=print, debug=True) as scanner:
        t0 = time.time()
        rows = scanner.scan(img)
        elapsed = (time.time() - t0) * 1000
        print(f"\nВремя скана: {elapsed:.0f} мс")
        print(f"Распознано строк: {len(rows)}")
        for r in rows:
            print(f"  y={r.center_y:4d} conf={r.confidence:5.1f}  '{r.raw_text}'")
