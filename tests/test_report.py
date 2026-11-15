import re
import unittest

from tea_monitor.catalog import Product, Variant
from tea_monitor.changes import compute_changes
from tea_monitor.config import Config
from tea_monitor.report import (
    TELEGRAM_LIMIT, commit_summary, format_events, format_welcome, plural, step_summary,
)

CFG = Config()


def var(vid, qty, title="Box (75g)", price="48.00"):
    available = qty is None or qty > 0
    return Variant(id=vid, title=title, price=price, available=available, qty=qty,
                   status="low_stock" if available else "out_of_stock",
                   stock_text=f"Low stock: {qty} left" if available else "Out of stock")


def prod(pid, *variants, title=None):
    return Product(id=pid, handle=f"tea-{pid}", title=title or f"Tea {pid}", variants=list(variants))


class FormatTest(unittest.TestCase):
    def events(self):
        prev, _ = compute_changes({}, [prod(1, var(11, 0)), prod(2, var(21, 9)), prod(3, var(31, 2))], "t0")
        current = [
            prod(1, var(11, 4)),                                   # снова в наличии
            prod(2, var(21, 7)),                                   # 9 → 7
            prod(3, var(31, 0)),                                   # закончился
            prod(4, var(41, 5, title="Default Title", price="3000.00"), title="Liu Bao <1970s> & Co"),
        ]
        _, events = compute_changes(prev, current, "t1")
        return events

    def test_message_contents(self):
        (text, preview), = format_events(self.events(), CFG)
        self.assertIn("🆕 <b>Новые товары</b> (1)", text)
        self.assertIn("✅ <b>Снова в наличии</b> (1)", text)
        self.assertIn("📦 <b>Изменилось количество</b> (2)", text)
        self.assertIn("Liu Bao &lt;1970s&gt; &amp; Co</a> — 3000.00 MYR · 5 шт.", text)
        self.assertIn("— Box (75g): 4 шт. · 48.00 MYR", text)
        self.assertIn("📉 ", text)
        self.assertIn("9 шт. → 7 шт. (−2)", text)
        self.assertIn("❌ ", text)
        self.assertIn("2 шт. → нет в наличии", text)
        self.assertIn('href="https://chazhijitea.com/products/tea-2?variant=21"', text)
        self.assertEqual(preview, "https://chazhijitea.com/products/tea-4")
        # Только разрешённые Telegram HTML-теги
        self.assertEqual(set(re.findall(r"</?([a-z]+)", text)), {"a", "b"})

    def test_no_events_no_messages(self):
        self.assertEqual(format_events([], CFG), [])

    def test_long_reports_are_split(self):
        prev, _ = compute_changes({}, [prod(i, var(i, 50)) for i in range(300)], "t0")
        _, events = compute_changes(prev, [prod(i, var(i, 49), title="Очень длинное название чая " * 3)
                                           for i in range(300)], "t1")
        messages = format_events(events, CFG)
        self.assertGreater(len(messages), 1)
        for text, _ in messages:
            self.assertLessEqual(len(text), TELEGRAM_LIMIT)
            self.assertEqual(text.count("<a "), text.count("</a>"))
        self.assertTrue(messages[1][0].startswith("🍵 <i>(продолжение)</i>"))
        self.assertEqual(sum(t.count("📉") for t, _ in messages), 300)

    def test_product_lines_stay_together(self):
        products = [prod(i, *[var(i * 100 + j, 5, title=f"20{10 + j} / Cake") for j in range(8)],
                         title="Новый чай с длинным названием " * 2) for i in range(60)]
        _, events = compute_changes({"0": {"handle": "x", "title": "x", "variants": {}}}, products, "t")
        messages = format_events([e for e in events if e.kind == "new_product"], CFG)
        self.assertGreater(len(messages), 1)
        for text, _ in messages:
            body = [line for line in text.split("\n")[1:] if line]
            self.assertFalse(body[0].startswith("    "), "чанк начинается с варианта без товара")
            self.assertTrue(text.split("\n")[-1].startswith("    "))

    def test_plural(self):
        words = ("чай", "чая", "чаёв")
        self.assertEqual([plural(n, *words) for n in (1, 2, 5, 11, 21, 22, 66, 112)],
                         ["чай", "чая", "чаёв", "чаёв", "чай", "чая", "чаёв", "чаёв"])

    def test_welcome_and_summaries(self):
        stats = {"products": 66, "variants": 90, "available": 61, "unknown": 0}
        text = format_welcome(stats, CFG)
        self.assertIn("66 чаёв, 90 позиций полной упаковки; в наличии — 61.", text)
        events = self.events()
        self.assertEqual(commit_summary(events),
                         "Каталог: новых: 1, снова в наличии: 1, изменений количества: 2")
        self.assertEqual(commit_summary([]), "Каталог: обновление состояния")
        md = step_summary(CFG, stats, events, [prod(2, var(21, 7))], ["заметка"], 10, 0)
        self.assertIn("| [Tea 2](https://chazhijitea.com/products/tea-2?variant=21) | Box (75g) | 48.00 |", md)
        self.assertNotIn("<a ", md)


if __name__ == "__main__":
    unittest.main()
