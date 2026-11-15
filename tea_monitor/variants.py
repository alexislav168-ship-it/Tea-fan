"""Разделение вариантов товара на «полную упаковку» и пробники."""

from __future__ import annotations

import re
from typing import Iterable

from .config import Config

_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"(kg|кг|gr|гр|g|г)(?![a-zа-яё])"
_COUNT_WORDS = r"(?:pcs?|pieces?|pk|packs?|bags?|balls?|cakes?|шт\.?)"
_TIMES = r"[x×х*]"  # латинская x, знак умножения, кириллическая х, звёздочка

# «14pcs x 8g», «12pk x 8g», «7 x 357g»
_COUNT_X_WEIGHT = re.compile(rf"(\d+)\s*{_COUNT_WORDS}?\s*{_TIMES}\s*{_NUM}\s*{_UNIT}", re.I)
# «2g x 10 pack»
_WEIGHT_X_COUNT = re.compile(rf"{_NUM}\s*{_UNIT}\s*{_TIMES}\s*(\d+)", re.I)
# «(357g)», «1kg»
_WEIGHT = re.compile(rf"{_NUM}\s*{_UNIT}", re.I)


def _grams(number: str, unit: str) -> float:
    value = float(number.replace(",", "."))
    return value * 1000 if unit.lower() in ("kg", "кг") else value


def parse_total_grams(text: str) -> float | None:
    """Общий вес упаковки из названия варианта, в граммах (или None)."""
    if not text:
        return None
    m = _COUNT_X_WEIGHT.search(text)
    if m:
        return int(m.group(1)) * _grams(m.group(2), m.group(3))
    m = _WEIGHT_X_COUNT.search(text)
    if m:
        return _grams(m.group(1), m.group(2)) * int(m.group(3))
    m = _WEIGHT.search(text)
    if m:
        return _grams(m.group(1), m.group(2))
    return None


def variant_text(variant: dict) -> str:
    """Название варианта: значения опций через « / » (или поле title)."""
    parts = [variant.get(f"option{i}") for i in (1, 2, 3)]
    parts = [str(p) for p in parts if p]
    return " / ".join(parts) if parts else str(variant.get("title") or "")


def _keyword_patterns(keywords: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(r"(?<![\w])" + re.escape(k.lower()), re.I) for k in keywords if k.strip()]


def sample_variant_ids(variants: list[dict], cfg: Config) -> set[int]:
    """Возвращает id вариантов-пробников в карточке товара.

    Пробник — вариант, в названии которого есть слово-маркер (Pocket-sized,
    Sample, Пробник…), либо лёгкий вариант (не тяжелее sample_max_grams),
    рядом с которым в той же карточке есть вариант хотя бы вдвое тяжелее.
    """
    patterns = _keyword_patterns(cfg.sample_keywords)
    texts = {v["id"]: variant_text(v) for v in variants}
    weights = {vid: parse_total_grams(text) for vid, text in texts.items()}
    samples: set[int] = set()
    for vid, text in texts.items():
        if any(p.search(text) for p in patterns):
            samples.add(vid)
            continue
        weight = weights[vid]
        if weight is None or weight > cfg.sample_max_grams:
            continue
        if any(w is not None and w >= 2 * weight for other, w in weights.items() if other != vid):
            samples.add(vid)
    return samples
