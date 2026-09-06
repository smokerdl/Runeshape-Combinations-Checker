"""
config.py

Загрузка/сохранение конфигурации программы. Эквивалент AppConfig.cs/ConfigStore.cs.
См. ТЗ раздел 10.

Список доступных лиг PoE2 больше не хранится в конфиге жёстко.
Он автоматически загружается с poe.ninja при запуске модуля и кэшируется
локально, чтобы временная недоступность API не ломала уже установленную программу.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests

DEFAULT_CONFIG_PATH = "config.json"
LEAGUE_CACHE_PATH = ".league_cache.json"
DEFAULT_HOTKEY = "f5"

_POE_NINJA_LEAGUES_URL = "https://poe.ninja/poe2/api/economy/leagues"
_LEAGUE_REQUEST_TIMEOUT = 10.0
_USER_AGENT = (
    "Runeshape-Combinations-Checker/1.0 "
    "(PoE2 economy client; https://github.com/smokerdl/Runeshape-Combinations-Checker)"
)


def _read_league_cache() -> list[dict[str, str]]:
    """Читает последний успешно сохранённый список лиг."""
    path = Path(LEAGUE_CACHE_PATH)
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    return _validate_league_data(data)


def _validate_league_data(data) -> list[dict[str, str]]:
    """Проверяет и нормализует ответ API/кэша со списком лиг."""
    if not isinstance(data, list):
        return []

    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()

    for entry in data:
        if not isinstance(entry, dict):
            continue

        league_id = entry.get("id")
        name = entry.get("name")
        if not isinstance(league_id, str) or not league_id.strip():
            continue
        if not isinstance(name, str) or not name.strip():
            continue

        league_id = league_id.strip()
        name = name.strip()

        if league_id in seen_ids or name in seen_names:
            continue

        seen_ids.add(league_id)
        seen_names.add(name)
        result.append({"id": league_id, "name": name})

    return result


def _save_league_cache(leagues: list[dict[str, str]]) -> None:
    """Сохраняет последний успешный ответ API для fallback при следующем запуске."""
    path = Path(LEAGUE_CACHE_PATH)
    try:
        path.write_text(
            json.dumps(leagues, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # Кэш не должен препятствовать запуску программы, если запись недоступна.
        pass


def _fetch_leagues_from_poe_ninja() -> list[dict[str, str]]:
    """Получает актуальные активные PoE2-лиги из официально документированного API poe.ninja."""
    headers = {"User-Agent": _USER_AGENT}
    response = requests.get(
        _POE_NINJA_LEAGUES_URL,
        headers=headers,
        timeout=_LEAGUE_REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    leagues = _validate_league_data(response.json())
    if not leagues:
        raise ValueError("poe.ninja вернул пустой или некорректный список лиг")

    _save_league_cache(leagues)
    return leagues


def _load_available_league_data() -> list[dict[str, str]]:
    """Сначала пытается получить свежий список, затем использует локальный кэш."""
    try:
        return _fetch_leagues_from_poe_ninja()
    except (requests.RequestException, ValueError, TypeError) as ex:
        cached = _read_league_cache()
        if cached:
            print(f"[config] не удалось получить список лиг с poe.ninja: {ex}; используется кэш")
            return cached

        print(f"[config] не удалось получить список лиг с poe.ninja: {ex}; кэш отсутствует")
        return []


# Единый автоматически определяемый источник доступных лиг.
_LEAGUE_DATA = _load_available_league_data()

# Названия для отображения в QComboBox.
AVAILABLE_LEAGUES = [entry["name"] for entry in _LEAGUE_DATA]

# Соответствие отображаемого имени league id из poe.ninja.
LEAGUE_IDS_BY_NAME = {entry["name"]: entry["id"] for entry in _LEAGUE_DATA}

# Первая запись API — текущая временная challenge-лига по документации poe.ninja.
DEFAULT_LEAGUE = AVAILABLE_LEAGUES[0] if AVAILABLE_LEAGUES else ""


@dataclass
class Region:
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.width, self.height)


@dataclass
class AppConfig:
    league_name: str = DEFAULT_LEAGUE
    region: Region = field(default_factory=Region)
    start_stop_hotkey: str = DEFAULT_HOTKEY

    @property
    def is_calibrated(self) -> bool:
        return self.region.is_valid


def load(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Загружает конфиг.

    Если сохранённой лиги больше нет среди актуальных лиг poe.ninja,
    автоматически выбирается первая лига из актуального списка (текущая
    временная challenge-лига). Это позволяет переживать смену сезона без
    ручного редактирования config.json.
    """
    p = Path(path)
    if not p.exists():
        return AppConfig()

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return AppConfig()

    region_data = data.get("region") or {}
    try:
        region = Region(
            x=int(region_data.get("x", 0)),
            y=int(region_data.get("y", 0)),
            width=int(region_data.get("width", 0)),
            height=int(region_data.get("height", 0)),
        )
    except (TypeError, ValueError):
        region = Region()

    saved_league = str(data.get("league_name", DEFAULT_LEAGUE))
    if AVAILABLE_LEAGUES and saved_league not in LEAGUE_IDS_BY_NAME:
        saved_league = DEFAULT_LEAGUE

    return AppConfig(
        league_name=saved_league,
        region=region,
        start_stop_hotkey=str(data.get("start_stop_hotkey", DEFAULT_HOTKEY)),
    )


def save(config: AppConfig, path: str = DEFAULT_CONFIG_PATH) -> None:
    data = {
        "league_name": config.league_name,
        "region": asdict(config.region),
        "start_stop_hotkey": config.start_stop_hotkey,
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Самопроверка: сохранение -> загрузка -> сверка ---
if __name__ == "__main__":
    import tempfile, os

    cfg = AppConfig(
        league_name=DEFAULT_LEAGUE,
        region=Region(x=43, y=145, width=508, height=548),
        start_stop_hotkey="f5",
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        save(cfg, path)
        loaded = load(path)

        assert loaded.league_name == cfg.league_name
        assert loaded.region == cfg.region
        assert loaded.start_stop_hotkey == cfg.start_stop_hotkey
        assert loaded.is_calibrated is True

        print("OK: сохранение/загрузка работают корректно")
        print(loaded)

    # Загрузка несуществующего файла -> конфиг по умолчанию, не калиброван.
    missing = load("/tmp/__does_not_exist__.json")
    assert missing.is_calibrated is False
    print("OK: отсутствующий файл -> конфиг по умолчанию, is_calibrated=False")
