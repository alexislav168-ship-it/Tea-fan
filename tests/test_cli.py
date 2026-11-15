import json
import tempfile
import unittest
from pathlib import Path

from tea_monitor.cli import chat_fingerprint, run
from tea_monitor.config import Config
from tea_monitor.shop import FetchError, InventoryBlock
from tea_monitor.telegram import TelegramError

CFG = Config()


class FakeShop:
    def __init__(self, products, stock):
        self.products = products  # сырые товары как в products.json
        self.stock = stock        # variant_id → текст наличия
        self.section_id = "template--1__main"
        self.requests_made = 0
        self.inventory_failures = 0
        self.fail = False

    def fetch_tea_products(self):
        if self.fail:
            raise FetchError("сайт недоступен")
        return self.products

    def fetch_inventory(self, handle, variant_id):
        self.requests_made += 1
        text = self.stock.get(variant_id)
        return None if text is None else InventoryBlock(True, text, self.section_id)


class FakeTelegram:
    instances = []

    def __init__(self, token, chats=None, fail_send=None):
        self.token = token
        self.sent = []
        self.chats = chats if chats is not None else [{"id": -42, "title": "Чайный движ"}]
        self.fail_send = fail_send
        FakeTelegram.instances.append(self)

    def get_me(self):
        return {"username": "tea_bot"}

    def find_chat(self, title):
        return next((c for c in self.chats if c["title"] == title), None)

    def send_message(self, chat_id, text, preview_url=None):
        if self.fail_send:
            raise self.fail_send
        self.sent.append((chat_id, text))
        return chat_id


def raw_product(pid, *variants):
    return {"id": pid, "handle": f"tea-{pid}", "title": f"Tea {pid}", "product_type": "Tea",
            "variants": [{"id": vid, "title": title, "option1": title, "price": "10.00", "available": avail}
                         for vid, title, avail in variants]}


def catalog():
    return [
        raw_product(1, (11, "Pocket-sized (10g)", False), (12, "Whole Cake (357g)", True)),
        raw_product(2, (21, "Box (75g)", False)),
    ]


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state" / "state.json"
        self.summary = Path(self.tmp.name) / "summary.md"
        self.output = Path(self.tmp.name) / "output.txt"
        FakeTelegram.instances = []

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, **extra):
        return {"GITHUB_STEP_SUMMARY": str(self.summary), "GITHUB_OUTPUT": str(self.output), **extra}

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_full_flow(self):
        shop = FakeShop(catalog(), {12: "Low stock: 5 left"})

        # 1. Первый запуск без Telegram: только исходная точка.
        result = run(CFG, self.state_path, env=self.env(), shop=shop, now="t0")
        self.assertTrue(result.first_run)
        self.assertEqual(result.messages, [])
        self.assertIn("TELEGRAM_BOT_TOKEN", " ".join(result.warnings))
        state = self.state()
        self.assertEqual(set(state["products"]), {"1", "2"})
        self.assertEqual(set(state["products"]["1"]["variants"]), {"12"})  # пробник не сохраняется
        self.assertEqual(state["products"]["1"]["variants"]["12"]["qty"], 5)
        self.assertIn("Каталог: обновление состояния", self.output.read_text(encoding="utf-8"))

        # 2. Подключили бота: приветствие + изменения.
        shop.products[1]["variants"][0]["available"] = True
        shop.stock[21] = "12 in stock"
        shop.stock[12] = "Low stock: 4 left"
        result = run(CFG, self.state_path, env=self.env(TELEGRAM_BOT_TOKEN="t"), shop=shop,
                     telegram_factory=FakeTelegram, now="t1")
        tg = FakeTelegram.instances[-1]
        self.assertEqual(len(tg.sent), 2)
        self.assertIn("Привет", tg.sent[0][1])
        self.assertIn("Снова в наличии", tg.sent[1][1])
        self.assertIn("5 шт. → 4 шт. (−1)", tg.sent[1][1])
        state = self.state()
        self.assertEqual(state["telegram"]["chat_id"], -42)
        self.assertEqual(state["telegram"]["welcomed"], chat_fingerprint(-42))

        # 3. Без изменений — тишина, приветствие не повторяется.
        run(CFG, self.state_path, env=self.env(TELEGRAM_BOT_TOKEN="t"), shop=shop,
            telegram_factory=FakeTelegram, now="t2")
        self.assertEqual(FakeTelegram.instances[-1].sent, [])

        # 4. Новый товар.
        shop.products.append(raw_product(3, (31, "Whole Cake (200g)", True)))
        shop.stock[31] = "3 in stock"
        run(CFG, self.state_path, env=self.env(TELEGRAM_BOT_TOKEN="t"), shop=shop,
            telegram_factory=FakeTelegram, now="t3")
        sent = FakeTelegram.instances[-1].sent
        self.assertEqual(len(sent), 1)
        self.assertIn("Новые товары", sent[0][1])
        self.assertIn("Tea 3", sent[0][1])

    def test_telegram_failure_keeps_state_for_retry(self):
        shop = FakeShop(catalog(), {12: "Low stock: 5 left"})
        run(CFG, self.state_path, env=self.env(), shop=shop, now="t0")
        before = self.state_path.read_text(encoding="utf-8")
        shop.stock[12] = "Low stock: 1 left"
        factory = lambda token: FakeTelegram(token, fail_send=TelegramError("timeout", 502))
        with self.assertRaises(TelegramError):
            run(CFG, self.state_path, env=self.env(TELEGRAM_BOT_TOKEN="t"), shop=shop,
                telegram_factory=factory, now="t1")
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_bot_not_in_chat_yet(self):
        shop = FakeShop(catalog(), {12: "Low stock: 5 left"})
        factory = lambda token: FakeTelegram(token, chats=[])
        result = run(CFG, self.state_path, env=self.env(TELEGRAM_BOT_TOKEN="t"), shop=shop,
                     telegram_factory=factory, now="t0")
        self.assertEqual(result.sent, 0)
        self.assertTrue(result.state_saved)
        self.assertIn("Чайный движ", " ".join(result.warnings))

    def test_chat_id_from_secret_is_not_stored(self):
        shop = FakeShop(catalog(), {12: "Low stock: 5 left"})
        run(CFG, self.state_path, env=self.env(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="-100777"),
            shop=shop, telegram_factory=FakeTelegram, now="t0")
        tg = FakeTelegram.instances[-1]
        self.assertEqual(tg.sent[0][0], -100777)
        text = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("100777", text)
        self.assertEqual(self.state()["telegram"]["welcomed"], chat_fingerprint(-100777))

    def test_bot_removed_from_chat(self):
        shop = FakeShop(catalog(), {12: "Low stock: 5 left"})
        run(CFG, self.state_path, env=self.env(TELEGRAM_BOT_TOKEN="t"), shop=shop,
            telegram_factory=FakeTelegram, now="t0")
        self.assertEqual(self.state()["telegram"]["chat_id"], -42)
        shop.stock[12] = "Low stock: 2 left"
        factory = lambda token: FakeTelegram(token, fail_send=TelegramError("Forbidden: bot was kicked", 403))
        result = run(CFG, self.state_path, env=self.env(TELEGRAM_BOT_TOKEN="t"), shop=shop,
                     telegram_factory=factory, now="t1")
        self.assertNotIn("chat_id", self.state()["telegram"])
        self.assertIn("снова", " ".join(result.warnings))

    def test_site_failure_raises_without_touching_state(self):
        shop = FakeShop(catalog(), {12: "Low stock: 5 left"})
        run(CFG, self.state_path, env=self.env(), shop=shop, now="t0")
        before = self.state_path.read_text(encoding="utf-8")
        shop.fail = True
        with self.assertRaises(FetchError):
            run(CFG, self.state_path, env=self.env(), shop=shop, now="t1")
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_dry_run_prints_and_does_not_save(self):
        shop = FakeShop(catalog(), {12: "Low stock: 5 left"})
        printed = []
        result = run(CFG, self.state_path, env=self.env(), shop=shop, dry_run=True,
                     test_message=True, out=printed.append, now="t0")
        self.assertFalse(self.state_path.exists())
        self.assertFalse(result.state_saved)
        self.assertTrue(any("Привет" in p for p in printed))
        self.assertTrue(any("Тестовое сообщение" in p for p in printed))


if __name__ == "__main__":
    unittest.main()
