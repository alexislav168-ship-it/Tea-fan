"""Настройки монитора: значения по умолчанию и загрузка из config.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_STATE_PATH = PROJECT_ROOT / "state" / "state.json"


@dataclass(frozen=True)
class Config:
    # Магазин (Shopify) и откуда брать список чаёв.
    shop_url: str = "https://chazhijitea.com"
    tea_collections: tuple[str, ...] = ("all-teas",)
    tea_product_types: tuple[str, ...] = ("Tea",)
    currency: str = "MYR"

    # Telegram-чат, который бот ищет по названию, если TELEGRAM_CHAT_ID не задан.
    telegram_chat_title: str = "Чайный движ"

    # Как отличить пробник от полной упаковки.
    # 1) по словам в названии варианта (сравнение с началом слова, без учёта регистра);
    sample_keywords: tuple[str, ...] = (
        "pocket", "sample", "tester", "trial", "taster", "single ball", "пробник",
    )
    # 2) по весу: вариант не тяжелее этого (в граммах) считается пробником,
    #    если в той же карточке есть вариант хотя бы вдвое тяжелее.
    sample_max_grams: float = 30.0

    # Вежливость к сайту: пауза между запросами и User-Agent.
    request_delay_seconds: float = 1.2
    request_timeout_seconds: float = 30.0
    user_agent: str = "Mozilla/5.0 (compatible; TeaStockMonitor/1.0)"


def load_config(path: str | Path | None = None) -> Config:
    """Читает config.json (если он есть) поверх значений по умолчанию."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {f.name: f for f in fields(Config)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ValueError(f"Неизвестные параметры в {path.name}: {', '.join(unknown)}")
    values = {}
    for key, value in raw.items():
        default = getattr(Config, key)
        if isinstance(default, tuple):
            if isinstance(value, str):
                value = [value]
            value = tuple(str(v) for v in value)
        elif isinstance(default, float):
            value = float(value)
        else:
            value = str(value)
        values[key] = value
    if "shop_url" in values:
        values["shop_url"] = values["shop_url"].rstrip("/")
    return Config(**values)
