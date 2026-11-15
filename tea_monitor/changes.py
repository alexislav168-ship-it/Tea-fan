"""Сравнение текущего каталога с сохранённым состоянием → список событий."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Product, Variant

# Виды событий
NEW_PRODUCT = "new_product"      # новый товар (карточка, которой раньше не было)
NEW_VARIANT = "new_variant"      # новый вариант у известного товара (например, другой год)
BACK_IN_STOCK = "back_in_stock"  # «нет в наличии» → «в наличии»
QTY_CHANGED = "qty_changed"      # изменилось количество на сайте
SOLD_OUT = "sold_out"            # «в наличии» → «нет в наличии»
REMOVED = "removed"              # товар/вариант в наличии пропал с сайта


class CatalogAnomaly(RuntimeError):
    """Каталог подозрительно опустел — скорее всего, сбой сайта, а не реальные изменения."""


@dataclass
class Event:
    kind: str
    product_id: str
    handle: str
    product_title: str
    variant_id: str | None = None
    variant: dict | None = None      # текущая запись варианта (для REMOVED — последняя известная)
    before: dict | None = None       # предыдущая запись варианта
    variants: list[tuple[str, dict]] = field(default_factory=list)  # для NEW_PRODUCT
    reappeared: bool = False         # для BACK_IN_STOCK: товар возвращается на сайт


def check_anomaly(prev_products: dict, products: list[Product]) -> None:
    active = {pid for pid, p in prev_products.items() if not p.get("removed_at")}
    if len(active) < 10:
        return
    current = {str(p.id) for p in products}
    still_there = len(active & current)
    if still_there < len(active) / 2:
        raise CatalogAnomaly(
            f"На сайте найдено только {still_there} из {len(active)} известных чаёв — "
            "похоже на сбой сайта, состояние не меняю."
        )


def _variant_record(variant: Variant, prev: dict | None, now: str) -> dict:
    record = {
        "title": variant.title,
        "price": variant.price,
        "available": variant.available,
        "qty": variant.qty,
        "status": variant.status,
        "stock_text": variant.stock_text,
        "first_seen": (prev or {}).get("first_seen", now),
    }
    if (
        variant.status == "unknown"
        and variant.available
        and prev
        and prev.get("available")
        and not prev.get("removed_at")
    ):
        # Наличие не удалось прочитать в этот раз — оставляем прошлые цифры.
        record.update(
            qty=prev.get("qty"),
            status=prev.get("status", "unknown"),
            stock_text=prev.get("stock_text", ""),
        )
    return record


def quantity_changed(before: dict, after: dict) -> bool:
    if before.get("status", "unknown") == "unknown" or after.get("status", "unknown") == "unknown":
        return False
    q1, q2 = before.get("qty"), after.get("qty")
    if q1 is not None and q2 is not None:
        return q1 != q2
    return (q1 is None) != (q2 is None)


def compute_changes(
    prev_products: dict[str, dict], products: list[Product], now: str
) -> tuple[dict[str, dict], list[Event]]:
    """Возвращает (новое состояние товаров, события)."""
    state: dict[str, dict] = {}
    events: list[Event] = []

    for product in products:
        pid = str(product.id)
        prev = prev_products.get(pid)
        prev_variants: dict[str, dict] = (prev or {}).get("variants", {})
        product_was_removed = bool(prev and prev.get("removed_at"))
        record = {
            "handle": product.handle,
            "title": product.title,
            "first_seen": (prev or {}).get("first_seen", now),
            "variants": {},
        }

        def event(kind: str, **kw) -> Event:
            return Event(kind=kind, product_id=pid, handle=product.handle,
                         product_title=product.title, **kw)

        new_product_variants: list[tuple[str, dict]] = []
        for variant in product.variants:
            vid = str(variant.id)
            before = prev_variants.get(vid)
            after = _variant_record(variant, before, now)
            record["variants"][vid] = after
            if prev is None:
                new_product_variants.append((vid, after))
                continue
            if before is None:
                events.append(event(NEW_VARIANT, variant_id=vid, variant=after))
                continue
            was_removed = product_was_removed or bool(before.get("removed_at"))
            was_available = bool(before.get("available")) and not was_removed
            if after["available"] and not was_available:
                events.append(event(BACK_IN_STOCK, variant_id=vid, variant=after, before=before,
                                    reappeared=was_removed))
            elif was_available and not after["available"]:
                events.append(event(SOLD_OUT, variant_id=vid, variant=after, before=before))
            elif was_available and quantity_changed(before, after):
                events.append(event(QTY_CHANGED, variant_id=vid, variant=after, before=before))

        # Варианты, которые пропали из карточки
        for vid, before in prev_variants.items():
            if vid in record["variants"]:
                continue
            gone = dict(before)
            gone.setdefault("removed_at", now)
            record["variants"][vid] = gone
            if not before.get("removed_at") and not product_was_removed and before.get("available"):
                events.append(event(REMOVED, variant_id=vid, variant=before, before=before))

        if prev is None:
            events.append(event(NEW_PRODUCT, variants=new_product_variants))
        state[pid] = record

    # Товары, которые пропали с сайта
    current_ids = {str(p.id) for p in products}
    for pid, prev in prev_products.items():
        if pid in current_ids:
            continue
        gone = dict(prev)
        gone.setdefault("removed_at", now)
        state[pid] = gone
        if prev.get("removed_at"):
            continue
        for vid, before in prev.get("variants", {}).items():
            if before.get("available") and not before.get("removed_at"):
                events.append(Event(kind=REMOVED, product_id=pid, handle=prev.get("handle", ""),
                                    product_title=prev.get("title", ""), variant_id=vid,
                                    variant=before, before=before))
    return state, events
