"""Чтение каталога и остатков с витрины Shopify (chazhijitea.com).

* Список товаров и признак «в наличии» — из /products.json и
  /collections/<коллекция>/products.json.
* Количество — ровно то, что показано на странице товара
  («20 in stock», «Low stock: 4 left», «Out of stock»). Чтобы не качать
  всю страницу, используется Section Rendering API Shopify: запрос
  /products/<handle>?variant=<id>&section_id=<секция> возвращает только
  блок товара с выбранным вариантом.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable

import requests

from .config import Config

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """Не удалось получить данные с сайта (после повторных попыток)."""


# --------------------------------------------------------------------------
# Разбор блока наличия на странице товара
# --------------------------------------------------------------------------

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_SKIP_TAGS = {"svg", "script", "style", "template"}


class _InventoryExtractor(HTMLParser):
    """Находит элемент наличия (id="Inventory-…" или class="product__inventory")."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found = False
        self.done = False
        self.section_id: str | None = None
        self.parts: list[str] = []
        self._depth = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self._depth:
            if tag in _VOID_TAGS:
                return
            self._depth += 1
            if self._skip or tag in _SKIP_TAGS:
                self._skip += 1
            return
        attrs = dict(attrs)
        el_id = attrs.get("id") or ""
        classes = (attrs.get("class") or "").split()
        if el_id.startswith("Inventory-") or "product__inventory" in classes:
            self.found = True
            self._depth = 1
            if el_id.startswith("Inventory-"):
                self.section_id = el_id[len("Inventory-"):]

    def handle_startendtag(self, tag, attrs):
        # Самозакрывающиеся теги (<circle/>) не меняют вложенность.
        return

    def handle_endtag(self, tag):
        if self.done or not self._depth or tag in _VOID_TAGS:
            return
        if self._skip:
            self._skip -= 1
        self._depth -= 1
        if self._depth == 0:
            self.done = True

    def handle_data(self, data):
        if self._depth and not self._skip and not self.done:
            self.parts.append(data)


@dataclass
class InventoryBlock:
    found: bool
    text: str
    section_id: str | None


def extract_inventory(html: str) -> InventoryBlock:
    parser = _InventoryExtractor()
    parser.feed(html)
    parser.close()
    text = " ".join("".join(parser.parts).split())
    return InventoryBlock(found=parser.found, text=text, section_id=parser.section_id)


_OUT_MARKERS = ("out of stock", "sold out", "unavailable", "нет в наличии", "распродан")
_LOW_MARKERS = ("low stock", "left", "осталось", "мало")
_IN_MARKERS = ("in stock", "available", "в наличии")


def parse_stock_text(text: str) -> tuple[int | None, str]:
    """Текст наличия → (количество или None, статус).

    Статусы: in_stock, low_stock, out_of_stock, unknown.
    """
    t = " ".join((text or "").split()).lower()
    if not t:
        return None, "unknown"
    if any(m in t for m in _OUT_MARKERS):
        return 0, "out_of_stock"
    m = re.search(r"\d[\d,\s]*", t)
    qty = int(re.sub(r"\D", "", m.group())) if m else None
    if any(m in t for m in _LOW_MARKERS):
        return qty, "low_stock"
    if qty is not None or any(m in t for m in _IN_MARKERS):
        return qty, "in_stock"
    return None, "unknown"


# --------------------------------------------------------------------------
# HTTP-клиент
# --------------------------------------------------------------------------

class ShopClient:
    def __init__(
        self,
        cfg: Config,
        section_id: str | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.section_id = section_id
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": cfg.user_agent,
            "Accept-Language": "en",
        })
        self._sleep = sleep
        self._clock = clock
        self._last_request = None
        self._consecutive_failures = 0
        self.requests_made = 0
        self.inventory_failures = 0

    # -- низкоуровневое ---------------------------------------------------

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.cfg.request_delay_seconds - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def get(self, path: str, params: dict | None = None, attempts: int = 4) -> requests.Response:
        """GET с паузами между запросами и повторами при 429/5xx/обрывах."""
        url = path if path.startswith("http") else f"{self.cfg.shop_url}{path}"
        backoff = [10, 30, 60]
        last_error = ""
        for attempt in range(1, attempts + 1):
            self._throttle()
            self.requests_made += 1
            try:
                response = self.session.get(url, params=params, timeout=self.cfg.request_timeout_seconds)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last_error = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit() and attempt < attempts:
                    delay = min(int(retry_after), 120)
                    log.info("%s → %s, жду %s с", url, last_error, delay)
                    self._sleep(delay)
                    continue
            if attempt < attempts:
                delay = backoff[min(attempt - 1, len(backoff) - 1)]
                log.info("%s → %s, повтор через %s с", url, last_error, delay)
                self._sleep(delay)
        raise FetchError(f"{url}: {last_error}")

    def get_json(self, path: str, params: dict | None = None) -> dict:
        response = self.get(path, params=params)
        if response.status_code != 200:
            raise FetchError(f"{path}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"{path}: ответ не JSON ({exc})") from exc

    # -- каталог -----------------------------------------------------------

    def _paged_products(self, path: str) -> list[dict]:
        products: list[dict] = []
        for page in range(1, 41):
            data = self.get_json(path, params={"limit": 250, "page": page})
            batch = data.get("products")
            if not isinstance(batch, list):
                raise FetchError(f"{path}: нет списка products")
            products.extend(batch)
            if len(batch) < 250:
                return products
        raise FetchError(f"{path}: слишком много страниц")

    def fetch_tea_products(self) -> list[dict]:
        """Все чаи: товары из чайных коллекций + товары с чайным типом."""
        found: dict[int, dict] = {}
        for handle in self.cfg.tea_collections:
            for product in self._paged_products(f"/collections/{handle}/products.json"):
                found[product["id"]] = product
        if self.cfg.tea_product_types:
            wanted = {t.strip().lower() for t in self.cfg.tea_product_types}
            for product in self._paged_products("/products.json"):
                if (product.get("product_type") or "").strip().lower() in wanted:
                    found.setdefault(product["id"], product)
        return list(found.values())

    # -- наличие -------------------------------------------------------------

    def fetch_inventory(self, handle: str, variant_id: int) -> InventoryBlock | None:
        """Блок наличия для конкретного варианта или None, если не получилось."""
        if self._consecutive_failures >= 3:
            # Сайт явно не отвечает/ограничивает — не долбим его дальше в этом запуске.
            self.inventory_failures += 1
            return None
        try:
            block = self._fetch_inventory(handle, variant_id)
        except FetchError as exc:
            log.warning("Не удалось получить наличие %s (%s): %s", handle, variant_id, exc)
            block = None
        if block is None:
            self._consecutive_failures += 1
            self.inventory_failures += 1
        else:
            self._consecutive_failures = 0
        return block

    def _fetch_inventory(self, handle: str, variant_id: int) -> InventoryBlock | None:
        path = f"/products/{handle}"
        if self.section_id:
            response = self.get(path, params={"variant": variant_id, "section_id": self.section_id})
            if response.status_code == 200:
                block = extract_inventory(response.text)
                if block.found:
                    return block
            elif response.status_code == 404:
                log.info("Секция %s не найдена для %s, читаю страницу целиком", self.section_id, handle)
        # Полная страница: заодно узнаём актуальный id секции.
        response = self.get(path, params={"variant": variant_id})
        if response.status_code != 200:
            return None
        block = extract_inventory(response.text)
        if not block.found:
            return None
        if block.section_id:
            self.section_id = block.section_id
        return block
