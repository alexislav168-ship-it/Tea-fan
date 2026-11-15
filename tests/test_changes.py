import unittest

from tea_monitor.catalog import Product, Variant
from tea_monitor.changes import (
    BACK_IN_STOCK, NEW_PRODUCT, NEW_VARIANT, QTY_CHANGED, REMOVED, SOLD_OUT,
    CatalogAnomaly, check_anomaly, compute_changes,
)

T0, T1 = "2026-11-15T10:00:00Z", "2026-11-15T10:10:00Z"


def var(vid, qty, title="Whole Cake (357g)", status=None, available=None, price="100.00"):
    if available is None:
        available = qty is None or qty > 0
    if status is None:
        status = "out_of_stock" if not available else ("in_stock" if qty is None or qty > 10 else "low_stock")
    return Variant(id=vid, title=title, price=price, available=available, qty=qty, status=status,
                   stock_text=f"{qty} in stock" if available else "Out of stock")


def prod(pid, *variants, title=None):
    return Product(id=pid, handle=f"tea-{pid}", title=title or f"Tea {pid}", variants=list(variants))


def baseline(*products):
    state, events = compute_changes({}, list(products), T0)
    return state


class ComputeChangesTest(unittest.TestCase):
    def kinds(self, events):
        return sorted((e.kind, e.product_id, e.variant_id) for e in events)

    def test_first_run_marks_everything_new(self):
        state, events = compute_changes({}, [prod(1, var(11, 5))], T0)
        self.assertEqual([e.kind for e in events], [NEW_PRODUCT])
        self.assertEqual(state["1"]["variants"]["11"]["qty"], 5)
        self.assertEqual(state["1"]["first_seen"], T0)

    def test_no_changes(self):
        prev = baseline(prod(1, var(11, 5), var(12, 0)))
        state, events = compute_changes(prev, [prod(1, var(11, 5), var(12, 0))], T1)
        self.assertEqual(events, [])
        self.assertEqual(state, prev)

    def test_new_product_and_new_variant(self):
        prev = baseline(prod(1, var(11, 5)))
        _, events = compute_changes(prev, [prod(1, var(11, 5), var(12, 3, title="2025 / Box")),
                                           prod(2, var(21, 0), var(22, 7))], T1)
        self.assertEqual(self.kinds(events), [(NEW_PRODUCT, "2", None), (NEW_VARIANT, "1", "12")])
        new = next(e for e in events if e.kind == NEW_PRODUCT)
        self.assertEqual([vid for vid, _ in new.variants], ["21", "22"])

    def test_back_in_stock(self):
        prev = baseline(prod(1, var(11, 0)))
        state, events = compute_changes(prev, [prod(1, var(11, 4))], T1)
        self.assertEqual(self.kinds(events), [(BACK_IN_STOCK, "1", "11")])
        self.assertEqual(state["1"]["variants"]["11"]["first_seen"], T0)

    def test_quantity_change_and_sold_out(self):
        prev = baseline(prod(1, var(11, 9), var(12, 2), var(13, 20)))
        _, events = compute_changes(prev, [prod(1, var(11, 7), var(12, 0), var(13, 20))], T1)
        self.assertEqual(self.kinds(events), [(QTY_CHANGED, "1", "11"), (SOLD_OUT, "1", "12")])
        qty = next(e for e in events if e.kind == QTY_CHANGED)
        self.assertEqual((qty.before["qty"], qty.variant["qty"]), (9, 7))

    def test_count_appears_or_disappears(self):
        prev = baseline(prod(1, var(11, None)))
        _, events = compute_changes(prev, [prod(1, var(11, 8))], T1)
        self.assertEqual(self.kinds(events), [(QTY_CHANGED, "1", "11")])

    def test_unknown_inventory_keeps_previous_numbers(self):
        prev = baseline(prod(1, var(11, 9)))
        state, events = compute_changes(prev, [prod(1, var(11, None, status="unknown", available=True))], T1)
        self.assertEqual(events, [])
        self.assertEqual(state["1"]["variants"]["11"]["qty"], 9)
        self.assertEqual(state["1"]["variants"]["11"]["status"], "low_stock")

    def test_unknown_after_out_of_stock_is_back_in_stock_without_count(self):
        prev = baseline(prod(1, var(11, 0)))
        state, events = compute_changes(prev, [prod(1, var(11, None, status="unknown", available=True))], T1)
        self.assertEqual(self.kinds(events), [(BACK_IN_STOCK, "1", "11")])
        self.assertIsNone(state["1"]["variants"]["11"]["qty"])
        # Следующая проверка узнаёт число — это не «изменение количества».
        _, events = compute_changes(state, [prod(1, var(11, 6))], T1)
        self.assertEqual(events, [])

    def test_removed_and_reappeared(self):
        prev = baseline(prod(1, var(11, 3), var(12, 0)), prod(2, var(21, 5)))
        state, events = compute_changes(prev, [prod(2, var(21, 5))], T1)
        self.assertEqual(self.kinds(events), [(REMOVED, "1", "11")])
        self.assertEqual(state["1"]["removed_at"], T1)

        # Пока товара нет — повторно не сообщаем.
        state2, events = compute_changes(state, [prod(2, var(21, 5))], "later")
        self.assertEqual(events, [])
        self.assertEqual(state2["1"]["removed_at"], T1)

        # Вернулся в наличии.
        state3, events = compute_changes(state2, [prod(1, var(11, 2), var(12, 0)), prod(2, var(21, 5))], "x")
        self.assertEqual(self.kinds(events), [(BACK_IN_STOCK, "1", "11")])
        self.assertTrue(events[0].reappeared)
        self.assertNotIn("removed_at", state3["1"])

    def test_variant_removed(self):
        prev = baseline(prod(1, var(11, 3), var(12, 4)))
        state, events = compute_changes(prev, [prod(1, var(11, 3))], T1)
        self.assertEqual(self.kinds(events), [(REMOVED, "1", "12")])
        self.assertEqual(state["1"]["variants"]["12"]["removed_at"], T1)

    def test_anomaly(self):
        prev = baseline(*[prod(i, var(i * 10, 1)) for i in range(1, 21)])
        with self.assertRaises(CatalogAnomaly):
            check_anomaly(prev, [prod(1, var(10, 1))])
        check_anomaly(prev, [prod(i, var(i * 10, 1)) for i in range(1, 12)])  # больше половины — ок
        check_anomaly({}, [])


if __name__ == "__main__":
    unittest.main()
