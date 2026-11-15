"""Хранение состояния каталога между запусками (JSON в репозитории)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_VERSION = 1


def load_state(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {"version": STATE_VERSION, "products": {}, "telegram": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("version", STATE_VERSION)
    data.setdefault("products", {})
    data.setdefault("telegram", {})
    return data


def _drop_none(value):
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None or k == "qty"}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def save_state(path: str | Path, state: dict) -> None:
    """Пишет состояние атомарно, с сортировкой ключей — чтобы диффы в git были минимальными."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_drop_none(state), ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
