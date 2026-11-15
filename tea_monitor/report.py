"""Тексты сообщений для Telegram (HTML) и сводка для GitHub Actions (Markdown)."""

from __future__ import annotations

import re
from html import escape, unescape
from urllib.parse import urlparse

from .changes import (
    BACK_IN_STOCK, NEW_PRODUCT, NEW_VARIANT, QTY_CHANGED, REMOVED, SOLD_OUT, Event,
)
from .config import Config

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3900  # с запасом под заголовок «(продолжение)»


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def shop_name(cfg: Config) -> str:
    return urlparse(cfg.shop_url).netloc or cfg.shop_url


def product_url(cfg: Config, handle: str, variant_id: str | None = None) -> str:
    url = f"{cfg.shop_url}/products/{handle}"
    return f"{url}?variant={variant_id}" if variant_id else url


def variant_label(title: str | None) -> str:
    title = (title or "").strip()
    return "" if title.lower() in ("", "default title") else title


def stock_phrase(record: dict) -> str:
    if not record.get("available"):
        return "нет в наличии"
    qty = record.get("qty")
    return f"{qty} шт." if qty is not None else "в наличии"


def price_phrase(record: dict, cfg: Config) -> str:
    price = (record.get("price") or "").strip()
    return f"{price} {cfg.currency}" if price else ""


def _link(cfg: Config, ev: Event, variant_id: str | None = None) -> str:
    url = product_url(cfg, ev.handle, variant_id)
    return f'<a href="{escape(url)}">{escape(ev.product_title)}</a>'


def _details(cfg: Config, record: dict, with_label: bool = True) -> str:
    parts = []
    label = variant_label(record.get("title"))
    if with_label and label:
        parts.append(escape(label))
    price = price_phrase(record, cfg)
    if price:
        parts.append(escape(price))
    parts.append(escape(stock_phrase(record)))
    return " · ".join(parts)


def _delta(before: dict, after: dict) -> str:
    q1, q2 = before.get("qty"), after.get("qty")
    if q1 is None or q2 is None or q1 == q2:
        return ""
    diff = q2 - q1
    return f" ({'+' if diff > 0 else '−'}{abs(diff)})"


def _was(before: dict) -> str:
    qty = before.get("qty")
    return f"{qty} шт." if qty is not None else "в наличии"


def _variant_prefix(record: dict) -> str:
    label = variant_label(record.get("title"))
    return f" — {escape(label)}:" if label else ":"


def _event_lines(cfg: Config, ev: Event) -> list[str]:
    if ev.kind == NEW_PRODUCT:
        if len(ev.variants) == 1 and not variant_label(ev.variants[0][1].get("title")):
            vid, rec = ev.variants[0]
            return [f"• {_link(cfg, ev, vid)} — {_details(cfg, rec, with_label=False)}"]
        lines = [f"• {_link(cfg, ev)}"]
        lines += [f"    {_details(cfg, rec)}" for _, rec in ev.variants]
        return lines
    rec = ev.variant or {}
    before = ev.before or {}
    link = _link(cfg, ev, ev.variant_id)
    if ev.kind == NEW_VARIANT:
        return [f"• {link} — новый вариант: {_details(cfg, rec)}"]
    if ev.kind == BACK_IN_STOCK:
        parts = [stock_phrase(rec)]
        if price_phrase(rec, cfg):
            parts.append(price_phrase(rec, cfg))
        tail = " (снова на сайте)" if ev.reappeared else ""
        return [f"• {link}{_variant_prefix(rec)} {escape(' · '.join(parts))}{tail}"]
    if ev.kind == QTY_CHANGED:
        q1, q2 = before.get("qty"), rec.get("qty")
        if q1 is not None and q2 is not None:
            icon = "📈" if q2 > q1 else "📉"
        else:
            icon = "🔄"
        return [f"{icon} {link}{_variant_prefix(rec)} {escape(_was(before))} → "
                f"{escape(stock_phrase(rec))}{_delta(before, rec)}"]
    if ev.kind == SOLD_OUT:
        return [f"❌ {link}{_variant_prefix(rec)} {escape(_was(before))} → нет в наличии"]
    if ev.kind == REMOVED:
        return [f"🚫 {link}{_variant_prefix(rec)} пропал с сайта (было {escape(_was(before))})"]
    return []


SECTIONS = (
    ("🆕", "Новые товары", (NEW_PRODUCT, NEW_VARIANT)),
    ("✅", "Снова в наличии", (BACK_IN_STOCK,)),
    ("📦", "Изменилось количество", (QTY_CHANGED, SOLD_OUT, REMOVED)),
)


def _preview_url(cfg: Config, events: list[Event]) -> str | None:
    for kinds in ((NEW_PRODUCT, NEW_VARIANT), (BACK_IN_STOCK,)):
        for ev in events:
            if ev.kind in kinds:
                return product_url(cfg, ev.handle)
    return None


def format_events(events: list[Event], cfg: Config) -> list[tuple[str, str | None]]:
    """События → список сообщений (HTML, url для превью ссылки или None)."""
    if not events:
        return []
    # «Единицы» — строки, которые нельзя разрывать между сообщениями:
    # заголовок раздела держится вместе с первым событием, товар — со своими вариантами.
    units: list[list[str]] = []
    for icon, title, kinds in SECTIONS:
        section = [ev for ev in events if ev.kind in kinds]
        if not section:
            continue
        head = ["", f"{icon} <b>{title}</b> ({len(section)})"]
        for i, ev in enumerate(section):
            lines = [_fit(line) for line in _event_lines(cfg, ev)]
            unit = head + lines if i == 0 else lines
            if _size(unit) > CHUNK_LIMIT:
                units.extend([line] for line in unit)
            else:
                units.append(unit)

    header = f"🍵 <b>Изменения на {escape(shop_name(cfg))}</b>"
    chunks: list[str] = []
    current = [header]
    for unit in units:
        if _size(current) + _size(unit) > CHUNK_LIMIT and len(current) > 1:
            chunks.append("\n".join(current))
            current = ["🍵 <i>(продолжение)</i>"]
        current.extend(unit)
    chunks.append("\n".join(current))

    preview = _preview_url(cfg, events)
    return [(chunk, preview if i == 0 else None) for i, chunk in enumerate(chunks)]


def _size(lines: list[str]) -> int:
    return sum(len(line) + 1 for line in lines)


def _fit(line: str) -> str:
    """Страховка от слишком длинной строки: без разметки и с обрезкой."""
    if len(line) <= CHUNK_LIMIT:
        return line
    return escape(_strip_html(line)[:CHUNK_LIMIT // 2]) + "…"


def _stats_line(stats: dict) -> str:
    p, v, a = stats["products"], stats["variants"], stats["available"]
    return (f"{p} {plural(p, 'чай', 'чая', 'чаёв')}, {v} {plural(v, 'позиция', 'позиции', 'позиций')} "
            f"полной упаковки; в наличии — {a}.")


def format_welcome(stats: dict, cfg: Config) -> str:
    return "\n".join([
        f"👋 Привет! Я слежу за каталогом чая на {escape(shop_name(cfg))} и пишу сюда, когда:",
        "🆕 появляется новый чай (или новый вариант, например другой год);",
        "✅ чай, которого не было в наличии, снова появляется в продаже;",
        "📦 меняется количество, указанное на сайте.",
        "",
        "Пробники (Pocket-sized и другие маленькие фасовки) не отслеживаю — только полную упаковку. "
        "Сайт проверяется автоматически, обычно несколько раз в час.",
        "",
        f"Сейчас под наблюдением: {_stats_line(stats)}",
    ])


def format_test(stats: dict, cfg: Config) -> str:
    return "\n".join([
        f"🧪 Тестовое сообщение: мониторинг {escape(shop_name(cfg))} работает.",
        f"Под наблюдением: {_stats_line(stats)}",
    ])


def commit_summary(events: list[Event]) -> str:
    counts = {}
    for ev in events:
        counts[ev.kind] = counts.get(ev.kind, 0) + 1
    parts = []
    new = counts.get(NEW_PRODUCT, 0) + counts.get(NEW_VARIANT, 0)
    if new:
        parts.append(f"новых: {new}")
    if counts.get(BACK_IN_STOCK):
        parts.append(f"снова в наличии: {counts[BACK_IN_STOCK]}")
    qty = counts.get(QTY_CHANGED, 0) + counts.get(SOLD_OUT, 0) + counts.get(REMOVED, 0)
    if qty:
        parts.append(f"изменений количества: {qty}")
    return "Каталог: " + (", ".join(parts) if parts else "обновление состояния")


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def step_summary(cfg: Config, stats: dict, events: list[Event], products, notes: list[str],
                 requests_made: int, inventory_failures: int) -> str:
    lines = [
        f"## 🍵 Мониторинг {shop_name(cfg)}",
        "",
        f"- Под наблюдением: {_stats_line(stats)}",
        f"- Запросов к сайту: {requests_made}; не удалось узнать количество: {inventory_failures}",
    ]
    lines += [f"- {note}" for note in notes]
    lines += ["", "### Изменения", ""]
    if events:
        for _, title, kinds in SECTIONS:
            section = [ev for ev in events if ev.kind in kinds]
            if not section:
                continue
            lines.append(f"**{title}**")
            for ev in section:
                for line in _event_lines(cfg, ev):
                    lines.append("- " + _strip_html(line).lstrip("• ").strip())
            lines.append("")
    else:
        lines += ["Изменений нет.", ""]
    lines += [
        "<details><summary>Текущее наличие (полная упаковка)</summary>",
        "",
        "| Чай | Вариант | Цена | Наличие на сайте |",
        "|---|---|---|---|",
    ]
    for product in products:
        for v in product.variants:
            stock = v.stock_text or ("Out of stock" if not v.available else "?")
            lines.append(f"| [{_md(product.title)}]({product_url(cfg, product.handle, str(v.id))}) "
                         f"| {_md(variant_label(v.title)) or '—'} | {_md(v.price)} | {_md(stock)} |")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def _strip_html(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", text))
