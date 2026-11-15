"""Минимальный клиент Telegram Bot API: поиск чата по названию и отправка сообщений."""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"


class TelegramError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None, parameters: dict | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.parameters = parameters or {}


def normalize_title(title: str | None) -> str:
    return " ".join((title or "").split()).casefold()


def parse_chat_id(value: str | int | None) -> int | str | None:
    """«-100123» → -100123, «@channel» → «@channel», пусто → None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return value


class TelegramClient:
    def __init__(self, token: str, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0) -> None:
        self._token = token.strip()
        self._session = session or requests.Session()
        self._sleep = sleep
        self._timeout = timeout

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def call(self, method: str, payload: dict | None = None, attempts: int = 3):
        url = f"{API_URL}/bot{self._token}/{method}"
        error = "нет ответа"
        for attempt in range(1, attempts + 1):
            try:
                response = self._session.post(url, json=payload or {}, timeout=self._timeout)
            except requests.RequestException as exc:
                error = self._redact(f"{type(exc).__name__}: {exc}")
                if attempt < attempts:
                    self._sleep(5 * attempt)
                continue
            try:
                data = response.json()
            except ValueError:
                error = f"HTTP {response.status_code}"
                if response.status_code >= 500 and attempt < attempts:
                    self._sleep(5 * attempt)
                    continue
                raise TelegramError(error, response.status_code)
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code")
            params = data.get("parameters") or {}
            description = self._redact(str(data.get("description") or "ошибка Telegram"))
            if attempt < attempts and code == 429 and params.get("retry_after"):
                self._sleep(min(float(params["retry_after"]), 60.0))
                continue
            if attempt < attempts and isinstance(code, int) and code >= 500:
                self._sleep(5 * attempt)
                continue
            raise TelegramError(f"{method}: {description}", code, params)
        raise TelegramError(f"{method}: {error}")

    def get_me(self) -> dict:
        return self.call("getMe")

    def find_chat(self, title: str) -> dict | None:
        """Ищет среди последних обновлений бота чат с нужным названием.

        Когда бота добавляют в группу/канал, Telegram присылает обновление
        my_chat_member с названием чата. Обновления хранятся 24 часа.
        """
        target = normalize_title(title)
        updates = self.call("getUpdates", {"timeout": 0}) or []
        chats: dict[int, dict] = {}
        migrations: dict[int, int] = {}
        for index, update in enumerate(updates):
            for key in ("my_chat_member", "message", "edited_message", "channel_post",
                        "edited_channel_post", "chat_member"):
                obj = update.get(key)
                if not isinstance(obj, dict):
                    continue
                chat = obj.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                if obj.get("migrate_to_chat_id"):
                    migrations[chat_id] = obj["migrate_to_chat_id"]
                entry = chats.setdefault(chat_id, {"chat": chat, "index": index, "status": None})
                entry["chat"], entry["index"] = chat, index
                if key == "my_chat_member":
                    entry["status"] = (obj.get("new_chat_member") or {}).get("status")
        candidates = [
            entry for entry in chats.values()
            if normalize_title(entry["chat"].get("title")) == target
            and entry["status"] not in ("left", "kicked")
        ]
        if not candidates:
            return None
        chat = dict(max(candidates, key=lambda e: e["index"])["chat"])
        seen = set()
        while chat["id"] in migrations and chat["id"] not in seen:
            seen.add(chat["id"])
            chat = {**chat, "id": migrations[chat["id"]], "type": "supergroup"}
        return chat

    def send_message(self, chat_id: int | str, text: str, preview_url: str | None = None) -> int | str:
        """Отправляет HTML-сообщение. Возвращает id чата (он меняется, если группа стала супергруппой)."""
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": (
                {"url": preview_url, "prefer_small_media": True} if preview_url else {"is_disabled": True}
            ),
        }
        try:
            self.call("sendMessage", payload)
            return chat_id
        except TelegramError as exc:
            new_id = exc.parameters.get("migrate_to_chat_id")
            if not new_id:
                raise
            log.info("Группа стала супергруппой, отправляю в новый чат")
            payload["chat_id"] = new_id
            self.call("sendMessage", payload)
            return new_id
