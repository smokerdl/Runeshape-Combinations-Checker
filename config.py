"""
config.py

Загрузка/сохранение конфигурации программы. Эквивалент AppConfig.cs/ConfigStore.cs.
См. ТЗ раздел 10.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_LEAGUE = "HC Runes of Aldur"
DEFAULT_HOTKEY = "f5"

# Список лиг для выпадающего списка в настройках. Названия лиг PoE2 меняются
# каждые несколько месяцев — если текущей лиги нет в списке, в settings_window.py
# предусмотреть возможность ввести название вручную (поле должно быть редактируемым,
# не строго ограниченным списком).
AVAILABLE_LEAGUES = [
    "HC Runes of Aldur",
    "Runes of Aldur",
    "Hardcore",
    "Standard",
]


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
    """Загружает конфиг. Если файла нет или он повреждён — возвращает конфиг
    по умолчанию (не калиброван), не бросая исключений — это нормальная ситуация
    при первом запуске."""
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

    return AppConfig(
        league_name=str(data.get("league_name", DEFAULT_LEAGUE)),
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
        league_name="HC Runes of Aldur",
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

    # Загрузка несуществующего файла -> конфиг по умолчанию, не калиброван
    missing = load("/tmp/__does_not_exist__.json")
    assert missing.is_calibrated is False
    print("OK: отсутствующий файл -> конфиг по умолчанию, is_calibrated=False")
