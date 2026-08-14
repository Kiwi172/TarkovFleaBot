"""Data shapes returned by the price source."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class VendorPrice:
    """A price offered by a named vendor (a trader, or ``Flea Market``)."""

    vendor: str
    price_rub: int
    currency: str = "RUB"

    @property
    def is_flea(self) -> bool:
        return self.vendor.strip().lower() in {"flea market", "flea-market", "flea"}


@dataclass(frozen=True, slots=True)
class ItemPrice:
    """A single item's pricing snapshot.

    ``price`` is the headline flea price used everywhere downstream; it is the
    lowest current listing where available, falling back to the 24h average.
    """

    item_id: str
    item_name: str
    short_name: str = ""
    price: int = 0
    quantity: int = 1
    avg_24h: int | None = None
    low_24h: int | None = None
    high_24h: int | None = None
    base_price: int | None = None
    change_48h_percent: float | None = None
    width: int = 1
    height: int = 1
    types: tuple[str, ...] = ()
    sell_for: tuple[VendorPrice, ...] = field(default_factory=tuple)

    @property
    def slots(self) -> int:
        """Grid slots the item occupies — used for per-slot profit ranking."""
        return max(1, self.width * self.height)

    @property
    def banned_from_flea(self) -> bool:
        return "noFlea" in self.types

    def best_trader(self) -> VendorPrice | None:
        """Highest-paying non-flea vendor, or None if the item has no buyer."""
        traders = [v for v in self.sell_for if not v.is_flea and v.price_rub > 0]
        return max(traders, key=lambda v: v.price_rub) if traders else None

    def as_dict(self) -> dict[str, object]:
        """The ``{item_name, price, quantity}`` shape, plus useful extras."""
        return {
            "item_name": self.item_name,
            "price": self.price,
            "quantity": self.quantity,
            "item_id": self.item_id,
            "short_name": self.short_name,
            "avg_24h": self.avg_24h,
            "slots": self.slots,
        }
