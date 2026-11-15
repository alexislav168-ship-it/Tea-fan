"""Один проход мониторинга: сайт → сравнение → Telegram → состояние."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .catalog import build_products, catalog_stats, fill_inventory
from .changes import CatalogAnomaly, Event, check_anomaly, compute_changes
from .config import DEFAULT_CONFIG_PATH, DEFAULT_STATE_PATH, Config, load_config
from .report import commit_summary, format_events, format_test, format_welcome, step_summary
from .shop import FetchError, ShopClient
from .state import STATE_VERSION, load_state, save_state
from .telegram import TelegramClient, TelegramError, parse_chat_id

log = logging.getLogger("tea_monitor")


def chat_fingerprint(chat_id) -> str:
    """Короткий отпечаток id чата — чтобы не хранить id из секрета в открытом виде."""
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:12]


@dataclass
class RunResult:
    events: list[Event]
    messages: list[tuple[str, str | None]] = field(default_factory=list)
    sent: int = 0
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    state_saved: bool = False
    first_run: bool = False


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def run(
    cfg: Config,
    state_path: str | Path,
    *,
    env: dict,
    dry_run: bool = False,
    save_in_dry_run: bool = False,
    test_message: bool = False,
    shop: ShopClient | None = None,
    telegram_factory: Callable[[str], TelegramClient] = TelegramClient,
    now: str | None = None,
    out: Callable[[str], None] = print,
) -> RunResult:
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = load_state(state_path)
    prev_products = state.get("products") or {}
    shop = shop or ShopClient(cfg, section_id=state.get("section_id"))

    # 1. Сайт
    products = build_products(shop.fetch_tea_products(), cfg)
    fill_inventory(products, shop)
    check_anomaly(prev_products, products)

    # 2. Сравнение
    new_products, events = compute_changes(prev_products, products, now)
    first_run = not prev_products
    if first_run:
        events = []
    stats = catalog_stats(products)
    telegram_state = dict(state.get("telegram") or {})
    new_state = {
        "version": STATE_VERSION,
        "shop_url": cfg.shop_url,
        "section_id": shop.section_id or state.get("section_id"),
        "telegram": telegram_state,
        "products": new_products,
    }
    result = RunResult(events=events, first_run=first_run)
    if first_run:
        result.notes.append("Первый запуск: каталог сохранён как исходная точка, "
                            "об изменениях начну сообщать со следующей проверки.")
    if shop.inventory_failures:
        result.warnings.append(f"Не удалось прочитать количество для {shop.inventory_failures} "
                               "позиций — для них оставлены прошлые значения.")

    # 3. Куда писать
    title = cfg.telegram_chat_title
    client: TelegramClient | None = None
    chat_id = None
    chat_from_secret = False
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if dry_run:
        result.notes.append("Режим --dry-run: сообщения только печатаются.")
    elif not token:
        result.warnings.append("Секрет TELEGRAM_BOT_TOKEN не задан — уведомления не отправляются "
                               "(инструкция в README).")
    else:
        client = telegram_factory(token)
        client.get_me()  # неверный токен → TelegramError → запуск завершается ошибкой
        chat_id = parse_chat_id(env.get("TELEGRAM_CHAT_ID"))
        chat_from_secret = chat_id is not None
        if chat_id is None:
            chat_id = parse_chat_id(telegram_state.get("chat_id"))
        if chat_id is None:
            try:
                chat = client.find_chat(title)
            except TelegramError as exc:
                chat = None
                result.warnings.append(f"Не удалось найти чат «{title}» через getUpdates ({exc}). "
                                       "Задайте секрет TELEGRAM_CHAT_ID.")
            if chat:
                chat_id = chat["id"]
                telegram_state["chat_id"] = chat_id
                result.notes.append(f"Найден чат «{chat.get('title')}», бот подключён.")
            else:
                result.warnings.append(
                    f"Бот пока не видит чат «{title}». Добавьте бота в группу с таким названием "
                    "(если уже добавлен — удалите и добавьте снова) и запустите проверку ещё раз.")

    # 4. Сообщения
    if dry_run or chat_id is not None:
        if dry_run or telegram_state.get("welcomed") != chat_fingerprint(chat_id):
            result.messages.append((format_welcome(stats, cfg), None))
        if test_message:
            result.messages.append((format_test(stats, cfg), None))
        result.messages.extend(format_events(events, cfg))

    if dry_run:
        for text, _ in result.messages:
            out(text)
            out("-" * 40)
    elif chat_id is not None and client is not None:
        try:
            for text, preview in result.messages:
                new_id = client.send_message(chat_id, text, preview_url=preview)
                if new_id != chat_id:
                    chat_id = new_id
                    if chat_from_secret:
                        result.warnings.append("Группа стала супергруппой — обновите секрет "
                                               "TELEGRAM_CHAT_ID (пока бот сам пишет в новый чат).")
                    else:
                        telegram_state["chat_id"] = new_id
                result.sent += 1
            telegram_state["welcomed"] = chat_fingerprint(chat_id)
        except TelegramError as exc:
            lost = exc.error_code == 403 or "chat not found" in str(exc).lower()
            if not (lost and not chat_from_secret and result.sent == 0):
                raise
            # Бота удалили из чата или чат удалён — забываем его и ищем заново в следующий раз.
            telegram_state.pop("chat_id", None)
            telegram_state.pop("welcomed", None)
            result.warnings.append(f"Бот не может писать в сохранённый чат ({exc}). "
                                   f"Добавьте бота в «{title}» снова.")

    # 5. Состояние и отчёты
    if not dry_run or save_in_dry_run:
        save_state(state_path, new_state)
        result.state_saved = True

    _append(env.get("GITHUB_STEP_SUMMARY"), step_summary(
        cfg, stats, events, products, [*result.warnings, *result.notes],
        shop.requests_made, shop.inventory_failures))
    _append(env.get("GITHUB_OUTPUT"), f"commit_message={commit_summary(events)}\n")
    return result


def _annotate(level: str, text: str, in_actions: bool) -> None:
    if in_actions:
        print(f"::{level}::{text}")
    else:
        print(f"[{level}] {text}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tea_monitor",
        description="Проверяет каталог чая на сайте и сообщает об изменениях в Telegram.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="путь к config.json")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="путь к файлу состояния")
    parser.add_argument("--dry-run", action="store_true",
                        help="ничего не отправлять в Telegram, а напечатать сообщения")
    parser.add_argument("--save", action="store_true",
                        help="вместе с --dry-run: всё равно сохранить состояние")
    parser.add_argument("--test-message", action="store_true",
                        help="дополнительно отправить тестовое сообщение")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    env = dict(os.environ)
    in_actions = env.get("GITHUB_ACTIONS") == "true"
    test_message = args.test_message or env.get("SEND_TEST_MESSAGE", "").strip().lower() == "true"
    cfg = load_config(args.config)

    try:
        result = run(cfg, args.state, env=env, dry_run=args.dry_run,
                     save_in_dry_run=args.save, test_message=test_message)
    except (FetchError, CatalogAnomaly) as exc:
        _annotate("error", f"Не удалось проверить сайт: {exc}", in_actions)
        _append(env.get("GITHUB_STEP_SUMMARY"), f"## ❌ Проверка не удалась\n\n{exc}\n")
        return 1
    except TelegramError as exc:
        _annotate("error", f"Ошибка Telegram: {exc}. Состояние не сохранено — "
                           "изменения будут отправлены при следующей проверке.", in_actions)
        _append(env.get("GITHUB_STEP_SUMMARY"), f"## ❌ Ошибка Telegram\n\n{exc}\n")
        return 1

    for note in result.notes:
        _annotate("notice", note, in_actions)
    for warning in result.warnings:
        _annotate("warning", warning, in_actions)
    log.info("Событий: %d, отправлено сообщений: %d, состояние %s.",
             len(result.events), result.sent, "сохранено" if result.state_saved else "не сохранялось")
    return 0
