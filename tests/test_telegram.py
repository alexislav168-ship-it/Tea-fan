import unittest

import requests

from tea_monitor.telegram import TelegramClient, TelegramError, parse_chat_id

TOKEN = "123456:SECRET-token"


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(result):
    return FakeResponse({"ok": True, "result": result})


def member_update(uid, chat_id, title, status="member", chat_type="group"):
    return {"update_id": uid, "my_chat_member": {
        "chat": {"id": chat_id, "title": title, "type": chat_type},
        "new_chat_member": {"status": status}}}


class TelegramTest(unittest.TestCase):
    def make(self, *responses):
        session = FakeSession(*responses)
        sleeps = []
        return TelegramClient(TOKEN, session=session, sleep=sleeps.append), session, sleeps

    def test_find_chat_by_title(self):
        updates = [
            member_update(1, -1, "Другой чат"),
            member_update(2, -2, "  чайный   ДВИЖ "),
            {"update_id": 3, "message": {"chat": {"id": -3, "title": "Чайный движ", "type": "group"},
                                          "text": "hi"}},
            member_update(4, -3, "Чайный движ", status="kicked"),
        ]
        tg, session, _ = self.make(ok(updates))
        chat = tg.find_chat("Чайный движ")
        self.assertEqual(chat["id"], -2)
        self.assertTrue(session.calls[0][0].endswith("/getUpdates"))

    def test_find_chat_follows_supergroup_migration(self):
        updates = [
            member_update(1, -5, "Чайный движ"),
            {"update_id": 2, "message": {"chat": {"id": -5, "title": "Чайный движ", "type": "group"},
                                          "migrate_to_chat_id": -1005}},
        ]
        tg, _, _ = self.make(ok(updates))
        self.assertEqual(tg.find_chat("Чайный движ")["id"], -1005)

    def test_single_group_with_other_title_is_used(self):
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 77, "type": "private", "first_name": "A"},
                                          "text": "/start"}},
            member_update(2, -7, "Cha Dao"),
        ]
        tg, _, _ = self.make(ok(updates))
        self.assertEqual(tg.find_chat("Чайный движ")["id"], -7)

    def test_single_group_after_migration_is_used(self):
        updates = [
            member_update(1, -5, "Cha Dao"),
            {"update_id": 2, "message": {"chat": {"id": -5, "title": "Cha Dao", "type": "group"},
                                          "migrate_to_chat_id": -1005}},
            {"update_id": 3, "message": {"chat": {"id": -1005, "title": "Cha Dao", "type": "supergroup"},
                                          "migrate_from_chat_id": -5}},
        ]
        tg, _, _ = self.make(ok(updates))
        self.assertEqual(tg.find_chat("Чайный движ")["id"], -1005)

    def test_find_chat_none(self):
        tg, _, _ = self.make(ok([]))
        self.assertIsNone(tg.find_chat("Чайный движ"))
        # Несколько групп, и ни одна не называется как нужно — не угадываем.
        tg, _, _ = self.make(ok([member_update(1, -1, "Другой"), member_update(2, -2, "Третий")]))
        self.assertIsNone(tg.find_chat("Чайный движ"))
        # Бота удалили из единственной группы.
        tg, _, _ = self.make(ok([member_update(1, -1, "Cha Dao", status="left")]))
        self.assertIsNone(tg.find_chat("Чайный движ"))

    def test_send_message_payload(self):
        tg, session, _ = self.make(ok({"message_id": 1}), ok({"message_id": 2}))
        self.assertEqual(tg.send_message(-2, "<b>hi</b>"), -2)
        tg.send_message(-2, "x", preview_url="https://example.com/p")
        payload = session.calls[0][1]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["link_preview_options"], {"is_disabled": True})
        self.assertEqual(session.calls[1][1]["link_preview_options"]["url"], "https://example.com/p")

    def test_send_message_after_migration(self):
        migrated = FakeResponse({"ok": False, "error_code": 400,
                                 "description": "Bad Request: group chat was upgraded to a supergroup chat",
                                 "parameters": {"migrate_to_chat_id": -1009}}, status=400)
        tg, session, _ = self.make(migrated, ok({"message_id": 3}))
        self.assertEqual(tg.send_message(-9, "x"), -1009)
        self.assertEqual(session.calls[1][1]["chat_id"], -1009)

    def test_rate_limit_retry(self):
        limited = FakeResponse({"ok": False, "error_code": 429, "description": "Too Many Requests",
                                "parameters": {"retry_after": 3}}, status=429)
        tg, _, sleeps = self.make(limited, ok({"message_id": 1}))
        tg.send_message(-1, "x")
        self.assertEqual(sleeps, [3.0])

    def test_errors_do_not_leak_token(self):
        error = requests.ConnectionError(f"failed https://api.telegram.org/bot{TOKEN}/getMe")
        tg, _, _ = self.make(error, error, error)
        with self.assertRaises(TelegramError) as ctx:
            tg.get_me()
        self.assertNotIn("SECRET", str(ctx.exception))

        bad = FakeResponse({"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401)
        tg, _, _ = self.make(bad)
        with self.assertRaises(TelegramError) as ctx:
            tg.get_me()
        self.assertEqual(ctx.exception.error_code, 401)

    def test_parse_chat_id(self):
        self.assertEqual(parse_chat_id(" -100123 "), -100123)
        self.assertEqual(parse_chat_id("@channel"), "@channel")
        self.assertIsNone(parse_chat_id(""))
        self.assertIsNone(parse_chat_id(None))


if __name__ == "__main__":
    unittest.main()
