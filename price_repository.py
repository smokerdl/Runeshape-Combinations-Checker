"""
price_repository.py

Получение и кэширование цен с poe.ninja. Эквивалент PriceRepository.cs.
См. ТЗ раздел 7.

ВАЖНО: poe.ninja требует заголовки User-Agent и Referer — без них API возвращает
пустой ответ ({"lines": [], "items": []}), даже не отдавая код ошибки. Это
подтверждено напрямую в ходе разработки (прямой запрос без заголовков либо через
инструменты без поддержки кастомных заголовков давал именно такой пустой ответ).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import requests


# --- Категории запросов к poe.ninja (см. ТЗ 7.2) ---
EXCHANGE_TYPES = ["Verisium", "Runes", "Expedition", "Currency", "UncutGems"]

# ИЗВЕСТНОЕ ОГРАНИЧЕНИЕ ИСТОЧНИКА ДАННЫХ (не баг программы): poe.ninja НЕ
# отслеживает категорию Skills/Supports (награды вида "Умение: .../Поддержка: ...")
# и некоторые редкие Greater-руны (напр. Greater Rune of Craft, Greater Rune of
# the Glacier). У таких предметов в принципе нет рыночных данных на сегодня —
# результат поиска цены будет "не найдено", это ожидаемое поведение.

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

_API_URL = "https://poe.ninja/poe2/api/economy/exchange/current/overview"

_DEFAULT_REFRESH_INTERVAL_SECONDS = 30 * 60  # 30 минут, см. ТЗ 7.4


def normalize(text: str) -> str:
    """Та же нормализация, что в ocr_scanner.py / ru_translator.py — единая для
    всего проекта, чтобы ключи словаря цен совпадали с переведёнными названиями."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


@dataclass(frozen=True)
class PriceEntry:
    divine_value: float
    exalted_value: float


class PriceRepository:
    """
    Загружает и кэширует цены с poe.ninja. Автообновление раз в 30 минут в
    фоновом потоке. При неудаче обновления — сохраняет предыдущий кэш (см. ТЗ 7.4).
    """

    def __init__(self, session: requests.Session | None = None, log=None, timeout: float = 15.0):
        self._session = session or requests.Session()
        self._log = log or (lambda msg: None)
        self._timeout = timeout

        self._prices: dict[str, PriceEntry] = {}
        self._lock = threading.Lock()
        self._last_fetched_at: datetime | None = None

        self._refresh_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Список callback-ов, вызываемых после каждого успешного обновления
        # (начальная загрузка + каждое фоновое). Вызывается из фонового потока —
        # если используется в UI-коде, нужно самостоятельно маршалить в UI-поток.
        self._on_updated_callbacks: list = []

    # --- Публичный API ---

    @property
    def prices(self) -> dict[str, PriceEntry]:
        with self._lock:
            return dict(self._prices)

    @property
    def item_count(self) -> int:
        with self._lock:
            return len(self._prices)

    @property
    def last_fetched_at(self) -> datetime | None:
        return self._last_fetched_at

    def on_prices_updated(self, callback) -> None:
        """Зарегистрировать callback, вызываемый после каждого успешного обновления цен."""
        self._on_updated_callbacks.append(callback)

    def initial_fetch(self, league_name: str) -> None:
        """Синхронная первая загрузка — блокирует, пока не получены цены (или не
        провалились все попытки). Вызывать при старте программы перед включением
        кнопки 'старт'."""
        self._fetch_and_merge(league_name)

    def start_auto_refresh(self, league_name: str, interval_seconds: int = _DEFAULT_REFRESH_INTERVAL_SECONDS) -> None:
        """Запускает фоновый поток автообновления раз в interval_seconds."""
        self.stop_auto_refresh()
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.wait(interval_seconds):
                self._fetch_and_merge(league_name)

        self._refresh_thread = threading.Thread(target=_loop, daemon=True)
        self._refresh_thread.start()

    def stop_auto_refresh(self) -> None:
        self._stop_event.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=2.0)
            self._refresh_thread = None

    # --- Внутренняя логика ---

    def _fetch_and_merge(self, league_name: str) -> None:
        merged: dict[str, PriceEntry] = {}
        any_success = False
        for exchange_type in EXCHANGE_TYPES:
            try:
                entries = self._fetch_type(league_name, exchange_type)
                merged.update(entries)
                any_success = True
            except requests.RequestException as ex:
                self._log(f"[PriceRepository] {exchange_type}: ошибка запроса: {ex}")
            except Exception as ex:
                self._log(f"[PriceRepository] {exchange_type}: ошибка обработки: {ex}")

        if not any_success:
            # Ни одна категория не загрузилась — сохраняем предыдущий кэш как есть,
            # не затираем его пустым результатом (см. ТЗ 7.4).
            self._log("[PriceRepository] обновление полностью провалилось, кэш не изменён")
            return

        with self._lock:
            self._prices = merged
        self._last_fetched_at = datetime.now()
        self._log(f"[PriceRepository] обновлено: {len(merged)} позиций")

        for cb in self._on_updated_callbacks:
            try:
                cb()
            except Exception as ex:
                self._log(f"[PriceRepository] ошибка в callback PricesUpdated: {ex}")

    def _fetch_type(self, league_name: str, exchange_type: str) -> dict[str, PriceEntry]:
        league_slug = league_name.replace(" ", "").lower()
        type_slug = exchange_type.lower()

        params = {"league": league_name, "type": exchange_type}
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": f"https://poe.ninja/poe2/economy/{league_slug}/{type_slug}",
        }

        resp = self._session.get(_API_URL, params=params, headers=headers, timeout=self._timeout)
        if not resp.ok:
            self._log(f"[PriceRepository] {exchange_type}: HTTP {resp.status_code}")
            return {}

        return self._parse_response(resp.json())

    @staticmethod
    def _parse_response(data: dict) -> dict[str, PriceEntry]:
        """
        Формат ответа poe.ninja (exchange/current/overview):
            items[]  -> {id, name}            — таблица id -> отображаемое имя
            lines[]  -> {id, primaryValue}    — цена в ПЕРВИЧНОЙ валюте лиги
            core.primary -> "divine" | "exalted" — в чём выражен primaryValue
            core.rates   -> {exalted, divine, chaos} — курсы пересчёта

        Первичная валюта зависит от лиги: софткорные лиги обычно ценят в divine,
        хардкорные — в exalted (divine слишком дорог для HC). Поэтому всегда
        вычисляем ОБА значения через core.rates, не предполагая заранее формат.
        """
        result: dict[str, PriceEntry] = {}

        items = data.get("items") or []
        name_map: dict[str, str] = {}
        for item in items:
            item_id = item.get("id")
            name = item.get("name")
            if item_id is not None and name is not None:
                name_map[item_id] = name

        core = data.get("core") or {}
        primary = core.get("primary", "divine")
        rates = core.get("rates") or {}

        divine_per_primary = 1.0 if primary == "divine" else float(rates.get("divine", 0) or 0)
        exalted_per_primary = 1.0 if primary == "exalted" else float(rates.get("exalted", 1) or 1)

        lines = data.get("lines") or []
        for line in lines:
            line_id = line.get("id")
            if line_id is None or line_id not in name_map:
                continue
            name = name_map[line_id]
            primary_value = float(line.get("primaryValue", 0) or 0)

            divine_value = primary_value * divine_per_primary
            exalted_value = round(primary_value * exalted_per_primary, 1)

            key = normalize(name)
            if key:
                result[key] = PriceEntry(divine_value=divine_value, exalted_value=exalted_value)

        return result


# --- Самопроверка парсинга на синтетических данных (без сети) ---
# Структура ответа взята из реальных проверенных логов C#-версии:
# "swift alloy" -> 0.01227 div, "regal orb" -> 0.001596 div, "exalted orb" -> 0.004865 div
if __name__ == "__main__":
    sample_response = {
        "core": {
            "primary": "exalted",
            "rates": {"divine": 0.005, "exalted": 1.0, "chaos": 150.0},
        },
        "items": [
            {"id": "swift-alloy", "name": "Swift Alloy"},
            {"id": "regal-orb", "name": "Regal Orb"},
            {"id": "exalted-orb", "name": "Exalted Orb"},
        ],
        "lines": [
            {"id": "swift-alloy", "primaryValue": 2.454},
            {"id": "regal-orb", "primaryValue": 0.3192},
            {"id": "exalted-orb", "primaryValue": 0.973},
        ],
    }

    parsed = PriceRepository._parse_response(sample_response)
    print("Распарсенные цены (синтетический тест, без сети):")
    for key, entry in parsed.items():
        print(f"  '{key}': divine={entry.divine_value:.6f}  exalted={entry.exalted_value:.3f}")

    print()
    print("Ожидалось примерно: swift alloy ~0.01227 div, regal orb ~0.0016 div, exalted orb ~0.0049 div")
    print("(сверено с реальными значениями из scan_log.txt C#-версии)")
