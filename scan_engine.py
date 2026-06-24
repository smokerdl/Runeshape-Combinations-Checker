"""
scan_engine.py

Главный цикл: захват экрана -> OCR -> перевод RU->EN -> поиск цены -> вывод
в оверлей. Эквивалент ScanEngine.cs. См. ТЗ раздел 8.

ВАЖНО — частота сканирования (изменено относительно исходного ТЗ): OCR-скан
(два прохода Tesseract) занимает ~900мс-1с (см. ocr_scanner.py), поэтому полный
скан выполняется НЕ ЧАЩЕ раза в секунду (MIN_SCAN_INTERVAL_SECONDS). Проверка
яркости региона (дешёвая операция) при этом может выполняться чаще — для более
отзывчивого определения открытия/закрытия панели.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image
from rapidfuzz.distance import Levenshtein

from ocr_scanner import OcrScanner, OcrRow
from ru_translator import RuTranslator
from price_repository import PriceRepository, PriceEntry
from screen_capture import capture_region


# --- Тайминги цикла ---
MIN_SCAN_INTERVAL_SECONDS = 1.0     # не чаще раза в секунду, см. договорённость по производительности
BRIGHTNESS_POLL_INTERVAL_SECONDS = 0.2   # дешёвая проверка яркости — чаще, для отзывчивости
HEARTBEAT_EVERY_N_POLLS = 25         # ~раз в 5 секунд при BRIGHTNESS_POLL_INTERVAL_SECONDS=0.2

# --- Гистерезис яркости (см. ТЗ 8.1 / ListDetector.cs) ---
OPEN_BRIGHTNESS = 100
CLOSE_BRIGHTNESS = 80
BRIGHT_STREAK_TO_OPEN = 2
DARK_STREAK_TO_CLOSE = 3
LEFT_FRACTION = 0.40
RIGHT_FRACTION = 0.98
ROW_FRACTIONS = (0.20, 0.35, 0.50, 0.65, 0.80)
SAMPLE_COLS = 12

# --- Анти-дребезг блокировки строк (см. ТЗ 8.2 / RowSlot в C#) ---
MERGE_Y_TOLERANCE = 20
CONFIRM_READS_FOR_FUZZY = 2     # нечёткие совпадения подтверждаются 2 одинаковыми чтениями подряд
EVICT_AFTER_MISSES = 3            # слот удаляется после стольких пропусков подряд

# --- Поиск цены по переведённому EN-имени (см. ТЗ 6.5) ---
PRICE_FUZZY_THRESHOLD = 0.84
PRICE_FUZZY_MAX_LEN_DIFF = 3
PRICE_PREFIX_MIN_LEN = 10

NOT_FOUND_MARKER = "—"


@dataclass
class PriceRow:
    center_y: int
    ocr_text: str
    name: str               # переведённое EN-имя (ключ) либо NOT_FOUND_MARKER
    multiplier: int
    has_price: bool
    divine_value: float = 0.0
    exalted_value: float = 0.0
    exact_match: bool = False


def _sample_brightness(image: Image.Image) -> int:
    """Среднее по сетке пикселей в правой части региона (см. ТЗ / ListDetector.cs).
    Иконки в левой ~30-40% дают тёмные провалы, искажающие среднее — поэтому
    сэмплируем только правую часть."""
    w, h = image.size
    x0 = int(w * LEFT_FRACTION)
    x1 = int(w * RIGHT_FRACTION)
    span = max(1, x1 - x0)
    px = image.load()

    total = 0
    count = 0
    for yf in ROW_FRACTIONS:
        cy = min(max(int(h * yf), 0), h - 1)
        for i in range(SAMPLE_COLS):
            cx = min(max(x0 + int((i + 0.5) * span / SAMPLE_COLS), 0), w - 1)
            r, g, b = px[cx, cy][:3]
            total += (r + g + b) // 3
            count += 1
    return total // max(1, count)


def _resolve_price(en_name: str, prices: dict[str, PriceEntry]) -> tuple[PriceEntry | None, bool]:
    """
    Поиск цены: точное -> префиксное -> нечёткое.
    КРИТИЧНО: для любого имени, содержащего "level" (самоцветы, Thaumaturgic Flux
    и т.п.) — ТОЛЬКО точное совпадение, без fallback'ов. Соседние уровни могут
    отличаться в цене в разы при разнице в названии в 1 символ (см. ТЗ 6.5).
    """
    if "level" in en_name:
        entry = prices.get(en_name)
        return (entry, True) if entry else (None, False)

    if en_name in prices:
        return prices[en_name], True

    if len(en_name) >= PRICE_PREFIX_MIN_LEN:
        candidates = [k for k in prices if k.startswith(en_name)]
        if candidates:
            best = min(candidates, key=len)
            return prices[best], False

    if len(en_name) >= 6:
        best_key = None
        best_score = PRICE_FUZZY_THRESHOLD
        for key in prices:
            if abs(len(key) - len(en_name)) > PRICE_FUZZY_MAX_LEN_DIFF:
                continue
            score = Levenshtein.normalized_similarity(en_name, key)
            if score > best_score:
                best_score = score
                best_key = key
        if best_key is not None:
            return prices[best_key], False

    return None, False


@dataclass
class _RowSlot:
    y: int
    latest: PriceRow | None = None
    locked: bool = False
    locked_row: PriceRow | None = None
    pending_name: str | None = None
    pending_count: int = 0
    unseen: int = 0


class ScanEngine:
    """
    Главный цикл сканирования. Запускается в отдельном потоке через start().
    on_state(rows, confirmed, reading) вызывается при каждом обновлении состояния
    оверлея — подключить отрисовку оверлея через этот callback.
    """

    def __init__(
        self,
        region: tuple[int, int, int, int],   # (x, y, width, height)
        tessdata_dir: str,
        json_path: str,
        price_repo: PriceRepository,
        log_path: str = "scan_log.txt",
        on_state=None,            # callback(rows: list[PriceRow], confirmed: bool, reading: bool)
        debug: bool = False,
    ):
        self._region = region
        self._tessdata_dir = tessdata_dir
        self._json_path = json_path
        self._price_repo = price_repo
        self._log_path = log_path
        self._on_state = on_state or (lambda rows, confirmed, reading: None)
        self._debug = debug

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._log_lock = threading.Lock()

    # --- Управление ---

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop_and_wait(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # --- Логирование ---

    def _log(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
        with self._log_lock:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        if self._debug:
            print(line)

    # --- Основной цикл ---

    def _run(self) -> None:
        Path(self._log_path).write_text("", encoding="utf-8")  # перезаписываем при каждом запуске

        x, y, w, h = self._region
        scanner = OcrScanner(self._tessdata_dir, log=self._log, debug=self._debug)
        translator = RuTranslator(self._json_path, log=self._log)

        if not translator.is_available:
            self._log("[RU] ВНИМАНИЕ: JSON база не загружена, перевод недоступен")
        else:
            self._log("[RU] переводчик активен")

        self._log(f"START region=({x},{y},{w},{h})")

        slots: dict[int, _RowSlot] = {}
        is_open = False
        confirmed_open = False
        bright_streak = 0
        dark_streak = 0
        last_ocr_at = 0.0
        poll_count = 0

        try:
            while not self._stop_event.is_set():
                poll_count += 1
                try:
                    region_img = capture_region(x, y, w, h)
                    brightness = _sample_brightness(region_img)
                    bright_frame = brightness > OPEN_BRIGHTNESS
                    dark_frame = brightness < CLOSE_BRIGHTNESS

                    if bright_frame:
                        bright_streak += 1
                        dark_streak = 0
                    elif dark_frame:
                        dark_streak += 1
                        bright_streak = 0
                    else:
                        bright_streak = 0
                        dark_streak = 0

                    prev_is_open = is_open
                    if not is_open and bright_streak >= BRIGHT_STREAK_TO_OPEN:
                        is_open = True
                    elif is_open and dark_streak >= DARK_STREAK_TO_CLOSE:
                        is_open = False

                    if poll_count % HEARTBEAT_EVERY_N_POLLS == 0:
                        self._log(
                            f"heartbeat poll={poll_count} open={is_open} confirmed={confirmed_open} "
                            f"brightness={brightness} rows={len(slots)}"
                        )

                    if is_open != prev_is_open:
                        self._log(f"panel {'OPEN' if is_open else 'CLOSED'} brightness={brightness}")
                        if not is_open:
                            slots.clear()
                            confirmed_open = False

                    if is_open:
                        now = time.monotonic()
                        if now - last_ocr_at >= MIN_SCAN_INTERVAL_SECONDS:
                            last_ocr_at = now
                            ocr_rows = scanner.scan(region_img)
                            if ocr_rows:
                                price_rows = self._build_price_rows(ocr_rows, translator)
                                if not confirmed_open and any(r.has_price for r in price_rows):
                                    confirmed_open = True
                                    self._log("panel CONFIRMED (найдена хотя бы одна цена)")
                                self._merge_rows(slots, price_rows)
                            else:
                                self._log("OCR вернул 0 строк")

                    reading = is_open and not confirmed_open
                    display_rows = self._collect_display_rows(slots)
                    self._on_state(display_rows, confirmed_open, reading)

                except Exception as ex:
                    self._log(f"ERROR {type(ex).__name__}: {ex}")

                self._stop_event.wait(BRIGHTNESS_POLL_INTERVAL_SECONDS)
        finally:
            scanner.close()
            self._on_state([], False, False)
            self._log("loop exited")

    # --- Построение строк с ценами из OCR-результатов ---

    def _build_price_rows(self, ocr_rows: list[OcrRow], translator: RuTranslator) -> list[PriceRow]:
        prices = self._price_repo.prices
        result: list[PriceRow] = []

        for row in ocr_rows:
            self._log(f"OCR: raw='{row.raw_text}' y={row.center_y} conf={row.confidence:.0f}")

            en_name, multiplier = translator.try_translate(row.raw_text)

            if en_name is None:
                result.append(PriceRow(
                    center_y=row.center_y, ocr_text=row.raw_text, name=NOT_FOUND_MARKER,
                    multiplier=multiplier, has_price=False,
                ))
                continue

            entry, exact = _resolve_price(en_name, prices)
            if entry is not None:
                result.append(PriceRow(
                    center_y=row.center_y, ocr_text=row.raw_text, name=en_name,
                    multiplier=multiplier, has_price=True,
                    divine_value=entry.divine_value * multiplier,
                    exalted_value=entry.exalted_value * multiplier,
                    exact_match=exact,
                ))
                self._log(f"  -> цена найдена: '{en_name}' x{multiplier} = {entry.divine_value * multiplier:.4f} div")
            else:
                result.append(PriceRow(
                    center_y=row.center_y, ocr_text=row.raw_text, name=en_name,
                    multiplier=multiplier, has_price=False,
                ))
                self._log(f"  -> переведено, но цена не найдена: '{en_name}' (нет данных у poe.ninja)")

        return result

    # --- Блокировка строк по позиции (анти-дребезг, см. ТЗ 8.2) ---

    def _merge_rows(self, slots: dict[int, _RowSlot], reads: list[PriceRow]) -> None:
        matched_ys: set[int] = set()

        for read in reads:
            slot = None
            best_d = None
            for sy, s in slots.items():
                if sy in matched_ys:
                    continue
                d = abs(sy - read.center_y)
                if d <= MERGE_Y_TOLERANCE and (best_d is None or d < best_d):
                    best_d = d
                    slot = s

            if slot is None:
                slot = _RowSlot(y=read.center_y)
                slots[slot.y] = slot

            matched_ys.add(slot.y)
            slot.unseen = 0
            slot.latest = read

            if read.has_price:
                if slot.pending_name == read.name:
                    slot.pending_count += 1
                else:
                    slot.pending_name = read.name
                    slot.pending_count = 1

                needed = 1 if read.exact_match else CONFIRM_READS_FOR_FUZZY
                if slot.pending_count >= needed:
                    if not slot.locked or (slot.locked_row and slot.locked_row.name != read.name):
                        self._log(f"locked y={slot.y} '{read.name}'")
                    slot.locked = True
                    slot.locked_row = read
            else:
                # Если цена не найдена, сбрасываем блокировку, 
                # чтобы отобразился маркер отсутствия цены (или иконка)
                slot.locked = False
                slot.locked_row = None
                slot.pending_name = None
                slot.pending_count = 0

        # Эвикция непойманных слотов
        for sy in list(slots.keys()):
            if sy not in matched_ys:
                slots[sy].unseen += 1
                if slots[sy].unseen > EVICT_AFTER_MISSES:
                    del slots[sy]

    @staticmethod
    def _collect_display_rows(slots: dict[int, _RowSlot]) -> list[PriceRow]:
        display = []
        for s in sorted(slots.values(), key=lambda s: s.y):
            if s.locked and s.locked_row is not None:
                display.append(s.locked_row)
            elif s.latest is not None:
                display.append(s.latest)
        return display