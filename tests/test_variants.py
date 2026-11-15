import json
import unittest
from pathlib import Path

from tea_monitor.catalog import build_products
from tea_monitor.config import Config
from tea_monitor.variants import parse_total_grams, sample_variant_ids, variant_text

FIXTURES = Path(__file__).parent / "fixtures"


def v(vid, *options, available=True, price="10.00"):
    opts = list(options) + [None] * (3 - len(options))
    return {"id": vid, "title": " / ".join(options), "option1": opts[0], "option2": opts[1],
            "option3": opts[2], "available": available, "price": price}


class ParseGramsTest(unittest.TestCase):
    def test_examples(self):
        cases = {
            "Pocket-sized (10g)": 10,
            "Box (75g)": 75,
            "2000s / Bag (1kg)": 1000,
            "2000s / Bag (100g)": 100,
            "2019 / Box (14pcs x 8g)": 112,
            "Box (2g x 10 pack)": 20,
            "Box (12pk x 8g)": 96,
            "Tong (7 x 357g)": 2499,
            "Whole Cake (400g) / Old Trunk (~300 years)": 400,
            "Пробник 25 г": 25,
            "Блин 357 гр": 357,
            "Box (16 Balls)": None,
            "Single Ball": None,
            "Default Title": None,
            "": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_total_grams(text), expected)


class SampleClassificationTest(unittest.TestCase):
    cfg = Config()

    def test_keywords(self):
        variants = [v(1, "2022", "Pocket-sized (10g)"), v(2, "2022", "Whole Cake (357g)"),
                    v(3, "Single Ball"), v(4, "Box (16 Balls)"), v(5, "Пробник")]
        self.assertEqual(sample_variant_ids(variants, self.cfg), {1, 3, 5})

    def test_small_weight_next_to_big_one_is_sample(self):
        variants = [v(1, "Mini (10g)"), v(2, "Cake (357g)")]
        self.assertEqual(sample_variant_ids(variants, self.cfg), {1})

    def test_single_small_product_is_not_sample(self):
        self.assertEqual(sample_variant_ids([v(1, "Box (2g x 10 pack)")], self.cfg), set())

    def test_regular_sizes_are_kept(self):
        variants = [v(1, "Bag (100g)"), v(2, "Bag (1kg)"), v(3, "Box (14pcs x 8g)")]
        self.assertEqual(sample_variant_ids(variants, self.cfg), set())

    def test_variant_text(self):
        self.assertEqual(variant_text(v(1, "2022", "Whole Cake (357g)")), "2022 / Whole Cake (357g)")
        self.assertEqual(variant_text({"id": 1, "title": "Default Title"}), "Default Title")

    def test_real_catalog_fixture(self):
        raw = json.loads((FIXTURES / "products.json").read_text(encoding="utf-8"))["products"]
        products = {p.handle: p for p in build_products(raw, self.cfg)}
        self.assertEqual(len(products), 10)
        full = {h: [x.title for x in p.variants] for h, p in products.items()}
        self.assertEqual(full["jing-zhi-shou-pu-erh-chang-yu-hao-2021-2022"],
                         ["2022 / Whole Cake (357g)", "2021 / Whole Cake (357g)"])
        self.assertEqual(full["xiao-qing-gan-pu-erh"], ["Box (16 Balls)"])
        self.assertEqual(full["wuyi-rock-tea-da-hong-pao-aged"], ["Box (12pk x 8g)"])
        self.assertEqual(full["black-tea-tea-bag-golden-series-cha-zhi-ji"], ["Box (2g x 10 pack)"])
        self.assertEqual(full["sshc-1970s-liu-bao-tea-purple-clay-jar-125g"], ["Default Title"])
        self.assertEqual(len(full["si-jin-qian-liu-bao-tea"]), 4)
        self.assertEqual(len(full["qian-gu-ling-ya-pu-erh-tea"]), 10)
        for p in products.values():
            for x in p.variants:
                self.assertNotIn("pocket", x.title.lower())


if __name__ == "__main__":
    unittest.main()
