"""
ru_translator.py

Переводит русские названия наград (считанные OCR) в нормализованные английские
названия через локальную базу runeshape_combinations.json. См. ТЗ раздел 6.

База данных хранится в "каноническом" виде — БЕЗ множителей количества (xN
срезаны скриптом clean_runeshape_json.py). Множитель читается отдельно с экрана
в виде суффикса "(N)" и применяется к цене после перевода, а не хранится в базе.
"""
from __future__ import annotations

import json
import re

from rapidfuzz.distance import Levenshtein


# --- Регулярные выражения ---

# Категория-префикс на экране перед именем умения/поддержки. В базе данных эти
# префиксы ОТСУТСТВУЮТ (проверено: 0 записей с "Умение:"/"Поддержка:" в JSON),
# поэтому их нужно срезать до поиска. Не привязан к началу строки — OCR-мусор
# от иконок часто стоит ПЕРЕД префиксом ("› | & Умение: ...").
_CATEGORY_PREFIX_RE = re.compile(r"(Умение|Поддержка)\s*:\s*", re.IGNORECASE)

# Множитель количества — число в круглых скобках СТРОГО В КОНЦЕ строки.
# "Чародейский расплав (Уровень 19) (1)" -> 1 (а не 19 — скобки с "Уровень"
# не на конце строки, поэтому не матчатся).
_MULTIPLIER_SUFFIX_RE = re.compile(r"(?<!\w)\((\d{1,3})\)\s*$")

# Тот же суффикс "(N)" в конце строки, для срезания перед поиском в словаре.
_QUANTITY_SUFFIX_RE = re.compile(r"\s*\(\d{1,3}\)\s*$")

# Слово "уровень" в нормализованном тексте — признак того, что для этого
# названия в базе могут быть СОСЕДНИЕ записи (другие уровни), отличающиеся на
# 1 символ. НЕ требуем строгого формата "уровень \d+\b" — если OCR исказил саму
# цифру до неузнаваемости (например "1я" вместо "19"), это НЕ повод снимать
# защиту, а ровно наоборот: чем хуже прочиталась цифра, тем выше риск, что
# нечёткий поиск выберет случайный соседний уровень. Поэтому проверяем только
# наличие самого слова "уровень" — этого достаточно, чтобы отключить шаг 2.
_LEVEL_WORD = "уровень"

# --- Пороги поиска (см. ТЗ 6.3, подтверждены тестами на C#-версии) ---
FUZZY_THRESHOLD = 0.82          # минимальная схожесть для нечёткого совпадения
FUZZY_MAX_LEN_DIFF = 5            # кандидаты с большей разницей длины не рассматриваются
SUFFIX_MAX_GARBAGE = 8           # макс. длина "мусора" спереди для совпадения по суффиксу
SUFFIX_MIN_KEY_LENGTH = 8        # мин. длина ключа для совпадения по суффиксу


def normalize(text: str) -> str:
    """Нормализация, единая для всех модулей проекта: нижний регистр, пунктуация
    -> пробел, схлопнуть пробелы. Python re по умолчанию Unicode-aware, поэтому
    \\w матчит кириллицу так же, как было в C#-версии."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def extract_multiplier(raw_text: str) -> int:
    """Извлекает множитель количества из конца строки.
    "Сфера хаоса (3)"                          -> 3
    "Чародейский расплав (Уровень 19) (1)"     -> 1
    "5 шт. случайной валюты"                   -> 1 (нет отдельных скобок в конце)
    """
    m = _MULTIPLIER_SUFFIX_RE.search(raw_text.strip())
    if m:
        n = int(m.group(1))
        return min(max(n, 1), 999)
    return 1


def strip_category_prefix(text: str) -> str:
    """Срезает 'Умение: '/'Поддержка: ' и всё, что было ДО них (мусор OCR от иконок)."""
    m = _CATEGORY_PREFIX_RE.search(text)
    return text[m.end():] if m else text


def strip_quantity_suffix(text: str) -> str:
    """Срезает суффикс '(N)' с конца строки."""
    return _QUANTITY_SUFFIX_RE.sub("", text.strip()).strip()


class RuTranslator:
    """
    Переводит сырой OCR-текст (RU) в нормализованное EN-название через JSON-базу.
    Три уровня поиска: точное -> нечёткое -> по суффиксу (см. ТЗ 6.3).
    """

    def __init__(self, json_path: str, log=None):
        self._log = log or (lambda msg: None)
        self._dict: dict[str, str] = {}
        self._load(json_path)

    def _load(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8") as f:
                entries = json.load(f)
        except Exception as ex:
            self._log(f"[RuTranslator] ошибка загрузки '{path}': {ex}")
            return

        for e in entries:
            ru_key = normalize(e["ru"])
            en_val = normalize(e["en"])
            if ru_key and en_val:
                self._dict[ru_key] = en_val
        self._log(f"[RuTranslator] загружено {len(self._dict)} записей из {path}")

    @property
    def is_available(self) -> bool:
        return len(self._dict) > 0

    def try_translate(self, raw_ocr_text: str) -> tuple[str | None, int]:
        """
        Возвращает (en_normalized_name, multiplier).
        en_normalized_name = None, если совпадение не найдено — вызывающий код
        должен показать "проверь json" вместо цены.
        """
        multiplier = extract_multiplier(raw_ocr_text)

        no_prefix = strip_category_prefix(raw_ocr_text)
        stripped = strip_quantity_suffix(no_prefix)
        ru_key = normalize(stripped)

        if not ru_key:
            return None, multiplier

        # КРИТИЧНО: если в названии есть номер уровня ("уровень N"), для него в базе
        # могут существовать СОСЕДНИЕ записи, отличающиеся на 1 символ (level 19 vs
        # level 10 -> схожесть 0.97 — выше порога 0.82!). Нечёткий поиск здесь даёт
        # правдоподобный, но В РАЗЫ неверный результат (соседний уровень = другая
        # цена). Поэтому для таких строк — ТОЛЬКО точное совпадение, без шага 2.
        # Совпадение по суффиксу (шаг 3) безопасно само по себе: оно требует точного
        # совпадения ХВОСТА строки, единичная опечатка в цифре уровня не пройдёт.
        has_level = _LEVEL_WORD in ru_key

        # 1. Точное совпадение
        if ru_key in self._dict:
            en = self._dict[ru_key]
            self._log(f"[RuTranslator] ТОЧНО '{raw_ocr_text.strip()}' -> '{en}' x{multiplier}")
            return en, multiplier

        # 2. Нечёткое совпадение — ПРОПУСКАЕТСЯ для строк с номером уровня
        if not has_level:
            best_key = self._best_fuzzy(ru_key)
            if best_key is not None:
                en = self._dict[best_key]
                score = Levenshtein.normalized_similarity(ru_key, best_key)
                self._log(
                    f"[RuTranslator] НЕЧЁТКО '{raw_ocr_text.strip()}' -> ключ='{best_key}' "
                    f"-> '{en}' x{multiplier} совпадение={score:.2f}"
                )
                return en, multiplier
        else:
            self._log(
                f"[RuTranslator] '{raw_ocr_text.strip()}' содержит номер уровня — "
                f"нечёткий поиск пропущен (защита от подмены уровня)"
            )

        # 3. Совпадение по суффиксу — безопасно даже для level-строк
        suffix_key = self._best_suffix(ru_key)
        if suffix_key is not None:
            en = self._dict[suffix_key]
            self._log(
                f"[RuTranslator] СУФФИКС '{raw_ocr_text.strip()}' -> ключ='{suffix_key}' "
                f"-> '{en}' x{multiplier}"
            )
            return en, multiplier

        self._log(f"[RuTranslator] НЕТ СОВПАДЕНИЯ для '{raw_ocr_text.strip()}' (ключ='{ru_key}')")
        return None, multiplier

    def _best_fuzzy(self, ru_key: str) -> str | None:
        best_key = None
        best_score = FUZZY_THRESHOLD
        for key in self._dict:
            if abs(len(key) - len(ru_key)) > FUZZY_MAX_LEN_DIFF:
                continue
            score = Levenshtein.normalized_similarity(ru_key, key)
            if score > best_score:
                best_score = score
                best_key = key
        return best_key

    def _best_suffix(self, ru_key: str) -> str | None:
        suffix_key = None
        for key in self._dict:
            if len(key) < SUFFIX_MIN_KEY_LENGTH or len(ru_key) <= len(key):
                continue
            garbage_len = len(ru_key) - len(key)
            if garbage_len > SUFFIX_MAX_GARBAGE:
                continue
            if ru_key.endswith(key):
                if suffix_key is None or len(key) > len(suffix_key):
                    suffix_key = key
        return suffix_key


# --- Самопроверка из командной строки ---
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print('Использование: python ru_translator.py <runeshape_combinations.json> "<сырой OCR текст>"')
        sys.exit(1)

    translator = RuTranslator(sys.argv[1], log=print)
    raw_text = sys.argv[2]
    en_name, mult = translator.try_translate(raw_text)
    print()
    print(f"Результат: en='{en_name}' multiplier={mult}")
