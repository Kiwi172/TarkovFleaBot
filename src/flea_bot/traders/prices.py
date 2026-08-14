"""Trader price reference and profit-margin computation.

Resolution order for an item's trader price:

1. an explicit override in ``config/trader_prices.yaml``,
2. the best trader ``sellFor`` entry from the API (if ``fall_back_to_api``),
3. no price — the item is skipped.

Blacklisted names are dropped before any of that.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from flea_bot.config import Config, get_config
from flea_bot.logging_setup import get_logger
from flea_bot.scraper.models import ItemPrice

log = get_logger("traders")


def normalise(name: str) -> str:
    """Canonical lookup key: lowercased, whitespace collapsed."""
    return re.sub(r"\s+", " ", name).strip().lower()


@dataclass(frozen=True, slots=True)
class TraderPrice:
    item_name: str
    price: int
    trader: str | None = None
    notes: str = ""
    # "override" (from YAML) or "api" (from sellFor).
    source: str = "override"


@dataclass(frozen=True, slots=True)
class MarginResult:
    """Profit for one item: what a trader pays minus what the flea charges."""

    item_id: str
    item_name: str
    flea_price: int
    trader_price: int
    trader: str | None
    price_source: str
    slots: int = 1
    quantity: int = 1

    @property
    def profit_margin(self) -> int:
        """The headline number: ``trader_price - flea_price``, per unit."""
        return self.trader_price - self.flea_price

    @property
    def margin_ratio(self) -> float:
        """Profit as a fraction of cost. 0.25 means a 25% return."""
        return self.profit_margin / self.flea_price if self.flea_price > 0 else 0.0

    @property
    def profit_per_slot(self) -> float:
        """Profit normalised by grid footprint — the real currency of a raid."""
        return self.profit_margin / self.slots

    @property
    def total_profit(self) -> int:
        return self.profit_margin * self.quantity

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_name": self.item_name,
            "flea_price": self.flea_price,
            "trader_price": self.trader_price,
            "trader": self.trader,
            "profit_margin": self.profit_margin,
            "margin_ratio": round(self.margin_ratio, 4),
            "profit_per_slot": round(self.profit_per_slot, 1),
            "price_source": self.price_source,
        }


class TraderPriceBook:
    """Loaded trader reference data with override + blacklist lookups."""

    def __init__(
        self,
        prices: dict[str, TraderPrice] | None = None,
        blacklist: Iterable[str] = (),
        *,
        meta: dict[str, Any] | None = None,
        config: Config | None = None,
    ) -> None:
        self.config = config or get_config()
        self._prices = prices or {}
        self._blacklist = {normalise(n) for n in blacklist}
        self.meta = meta or {}

    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: Path | str | None = None,
        *,
        config: Config | None = None,
    ) -> TraderPriceBook:
        """Read the YAML reference file. Missing file = empty book + warning."""
        cfg = config or get_config()
        target = Path(path) if path is not None else cfg.trader_reference_path()

        if not target.is_file():
            log.warning(
                "Trader reference {} not found — relying entirely on API trader prices.",
                target,
            )
            return cls(config=cfg)

        with target.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        prices: dict[str, TraderPrice] = {}
        for name, value in (raw.get("items") or {}).items():
            entry = _parse_entry(name, value)
            if entry is not None:
                prices[normalise(name)] = entry

        book = cls(
            prices=prices,
            blacklist=raw.get("blacklist") or (),
            meta=raw.get("meta") or {},
            config=cfg,
        )
        log.info(
            "Loaded {} trader override(s) and {} blacklist entr(ies) from {}",
            len(prices),
            len(book._blacklist),
            target.name,
        )
        return book

    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._prices)

    def is_blacklisted(self, item_name: str) -> bool:
        return normalise(item_name) in self._blacklist

    def override_for(self, item_name: str) -> TraderPrice | None:
        return self._prices.get(normalise(item_name))

    def resolve(self, item: ItemPrice) -> TraderPrice | None:
        """Best available trader price for an item, or None if unsellable."""
        if self.is_blacklisted(item.item_name):
            return None

        if (override := self.override_for(item.item_name)) is not None:
            return override

        if not self.config.traders.fall_back_to_api:
            return None

        best = item.best_trader()
        if best is None:
            return None
        return TraderPrice(
            item_name=item.item_name,
            price=best.price_rub,
            trader=best.vendor,
            source="api",
        )

    # ------------------------------------------------------------------
    def compute_margin(self, item: ItemPrice) -> MarginResult | None:
        """Profit margin for one item. None if it has no trader price."""
        trader_price = self.resolve(item)
        if trader_price is None or trader_price.price <= 0 or item.price <= 0:
            return None
        return MarginResult(
            item_id=item.item_id,
            item_name=item.item_name,
            flea_price=item.price,
            trader_price=trader_price.price,
            trader=trader_price.trader,
            price_source=trader_price.source,
            slots=item.slots,
            quantity=item.quantity,
        )

    def compute_margins(self, items: Iterable[ItemPrice]) -> list[MarginResult]:
        """Margins for every item that has one. Order is preserved."""
        results = [m for item in items if (m := self.compute_margin(item)) is not None]
        log.debug("Computed margins for {} item(s)", len(results))
        return results


def _parse_entry(name: str, value: Any) -> TraderPrice | None:
    """Accept either ``"Name": 1234`` or the full mapping form."""
    if isinstance(value, (int, float)):
        return TraderPrice(item_name=name, price=int(value))

    if isinstance(value, dict):
        price = value.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            log.warning("Trader entry {!r} has no usable 'price' — skipped.", name)
            return None
        return TraderPrice(
            item_name=name,
            price=int(price),
            trader=value.get("trader"),
            notes=str(value.get("notes") or ""),
        )

    log.warning("Trader entry {!r} has unexpected type {} — skipped.", name, type(value).__name__)
    return None


def profit_margin(flea_price: int, trader_price: int) -> int:
    """``trader_price - flea_price``. Exposed for direct use and testing."""
    return trader_price - flea_price
