import unittest
from pathlib import Path

import requests

from tea_monitor.config import Config
from tea_monitor.shop import FetchError, ShopClient, extract_inventory, parse_stock_text

FIXTURES = Path(__file__).parent / "fixtures"
SECTION = "template--28650271244580__main"


class FakeResponse:
    def __init__(self, status=200, text="", json_data=None, headers=None):
        self.status_code = status
        self.text = text
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class FakeSession:
    """Отвечает по очереди из списка; запоминает запросы."""

    def __init__(self, handler):
        self.headers = {}
        self.calls = []
        self._handler = handler

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        result = self._handler(url, dict(params or {}), len(self.calls))
        if isinstance(result, Exception):
            raise result
        return result


def inventory_html(text, section=SECTION):
    return (f'<div><p class="product__inventory" id="Inventory-{section}" role="status">'
            f'<span class="svg-wrapper"><svg><circle r="1"/></svg></span>{text}</p></div>')


def client(handler, **kw):
    sleeps = []
    session = FakeSession(handler)
    shop = ShopClient(Config(request_delay_seconds=0), session=session, sleep=sleeps.append, **kw)
    return shop, session, sleeps


class InventoryParsingTest(unittest.TestCase):
    def test_real_section_fixture(self):
        html = (FIXTURES / "section_low_stock.html").read_text(encoding="utf-8")
        block = extract_inventory(html)
        self.assertTrue(block.found)
        self.assertEqual(block.text, "Low stock: 9 left")
        self.assertEqual(block.section_id, SECTION)

    def test_missing_block(self):
        block = extract_inventory("<div><p>Hello</p></div>")
        self.assertFalse(block.found)

    def test_nested_and_void_tags(self):
        block = extract_inventory('<p class="product__inventory">Only <b>3</b><br> left'
                                  '<script>var x = "99 in stock";</script></p><p>12 in stock</p>')
        self.assertEqual(block.text, "Only 3 left")

    def test_stock_texts(self):
        cases = {
            "Low stock: 9 left": (9, "low_stock"),
            "48 in stock": (48, "in_stock"),
            "1,250 in stock": (1250, "in_stock"),
            "In stock": (None, "in_stock"),
            "Out of stock": (0, "out_of_stock"),
            "Sold out": (0, "out_of_stock"),
            "Нет в наличии": (0, "out_of_stock"),
            "В наличии: 5 шт.": (5, "in_stock"),
            "Осталось 2": (2, "low_stock"),
            "": (None, "unknown"),
            "Something else": (None, "unknown"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_stock_text(text), expected)


class ShopClientTest(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        def handler(url, params, n):
            if n == 1:
                return FakeResponse(429, headers={"Retry-After": "7"})
            if n == 2:
                return requests.ConnectionError("boom")
            return FakeResponse(200, json_data={"products": []})

        shop, session, sleeps = client(handler)
        self.assertEqual(shop.get_json("/products.json"), {"products": []})
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [7, 30])

    def test_gives_up_after_attempts(self):
        shop, session, _ = client(lambda url, params, n: FakeResponse(503))
        with self.assertRaises(FetchError):
            shop.get_json("/products.json")
        self.assertEqual(len(session.calls), 4)

    def test_tea_products_union_and_paging(self):
        page1 = [{"id": i, "product_type": "Tea"} for i in range(250)]

        def handler(url, params, n):
            if "/collections/all-teas/" in url:
                if params["page"] == 1:
                    return FakeResponse(200, json_data={"products": page1})
                return FakeResponse(200, json_data={"products": [{"id": 999, "product_type": ""}]})
            return FakeResponse(200, json_data={"products": [
                {"id": 1, "product_type": "Tea"},
                {"id": 5000, "product_type": "tea"},
                {"id": 6000, "product_type": "Teaware"},
            ]})

        shop, session, _ = client(handler)
        ids = {p["id"] for p in shop.fetch_tea_products()}
        self.assertIn(999, ids)
        self.assertIn(5000, ids)
        self.assertNotIn(6000, ids)
        self.assertEqual(len(ids), 252)

    def test_section_rendering_and_fallback(self):
        def handler(url, params, n):
            if "section_id" in params:
                if params["section_id"] == "template--old__main":
                    return FakeResponse(404)
                return FakeResponse(200, text=inventory_html(f"{params['variant']} in stock"))
            return FakeResponse(200, text="<html>" + inventory_html("Low stock: 3 left") + "</html>")

        shop, session, _ = client(handler, section_id="template--old__main")
        block = shop.fetch_inventory("tea", 11)
        self.assertEqual(block.text, "Low stock: 3 left")
        self.assertEqual(shop.section_id, SECTION)
        block = shop.fetch_inventory("tea", 12)
        self.assertEqual(block.text, "12 in stock")
        self.assertEqual(session.calls[-1][1], {"variant": 12, "section_id": SECTION})

    def test_stops_after_repeated_failures(self):
        shop, session, _ = client(lambda url, params, n: FakeResponse(404))
        for vid in range(5):
            self.assertIsNone(shop.fetch_inventory("tea", vid))
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(shop.inventory_failures, 5)


if __name__ == "__main__":
    unittest.main()
