"""Модель каталога: чаи и их варианты полной упаковки с наличием."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .shop import ShopClient, parse_stock_text
from .variants import sample_variant_ids, variant_text

log = logging.getLogger(__name__)


@dataclass
class Variant:
    id: int
    title: str
    price: str
    available: bool
    qty: int | None = None
    status: str = "unknown"  # in_stock | low_stock | out_of_stock | unknown
    stock_text: str = ""


@dataclass
class Product:
    id: int
    handle: str
    title: str
    variants: list[Variant]
    samples: list[str] = field(default_factory=list)


def build_products(raw_products: list[dict], cfg: Config) -> list[Product]:
    """Товары из JSON Shopify → только варианты полной упаковки (без пробников)."""
    products: list[Product] = []
    for raw in raw_products:
        raw_variants = raw.get("variants") or []
        samples = sample_variant_ids(raw_variants, cfg)
        full = [
            Variant(
                id=int(v["id"]),
                title=variant_text(v),
                price=str(v.get("price") or ""),
                available=bool(v.get("available")),
            )
            for v in raw_variants
            if v["id"] not in samples
        ]
        if not full:
            continue
        products.append(Product(
            id=int(raw["id"]),
            handle=str(raw["handle"]),
            title=str(raw.get("title") or raw["handle"]).strip(),
            variants=full,
            samples=[variant_text(v) for v in raw_variants if v["id"] in samples],
        ))
    products.sort(key=lambda p: p.title.casefold())
    return products


def fill_inventory(products: list[Product], shop: ShopClient) -> None:
    """Проставляет количество, показанное на сайте, каждому варианту."""
    for product in products:
        for variant in product.variants:
            if not variant.available:
                variant.qty, variant.status, variant.stock_text = 0, "out_of_stock", "Out of stock"
                continue
            block = shop.fetch_inventory(product.handle, variant.id)
            if block is None:
                variant.qty, variant.status, variant.stock_text = None, "unknown", ""
                continue
            qty, status = parse_stock_text(block.text)
            if status == "out_of_stock":
                # Страница и products.json противоречат друг другу — не верим ни одному.
                log.warning("%s / %s: в JSON «в наличии», на странице «%s»",
                            product.handle, variant.title, block.text)
                qty, status = None, "unknown"
            elif status == "unknown" and not block.text:
                # Магазин не ведёт учёт остатков для варианта — просто «в наличии».
                status = "in_stock"
            variant.qty, variant.status, variant.stock_text = qty, status, block.text


def catalog_stats(products: list[Product]) -> dict:
    variants = [v for p in products for v in p.variants]
    return {
        "products": len(products),
        "variants": len(variants),
        "available": sum(1 for v in variants if v.available),
        "unknown": sum(1 for v in variants if v.available and v.status == "unknown"),
    }
