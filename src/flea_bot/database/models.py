"""SQLAlchemy 2.0 schema for price history.

Two tables:

``items``
    One row per item — stable identity and metadata that rarely changes.

``price_snapshots``
    Append-only time series. Every fetch writes one row per item. Never
    updated, so rolling stats are just aggregates over a time window.

The ``(item_id, recorded_at)`` index is what makes the window queries fast;
without it the rolling-average query degrades to a full scan once you have a
few hundred thousand snapshots.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Timezone-aware UTC now. Naive datetimes in a time series are a trap."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"

    # tarkov.dev item id (a BSG template id), e.g. "5449016a4bdc2d6f028b456f".
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    short_name: Mapped[str] = mapped_column(String(64), default="")
    base_price: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer, default=1)
    height: Mapped[int] = mapped_column(Integer, default=1)
    # Comma-joined tarkov.dev type tags, e.g. "ammo,barter".
    types: Mapped[str] = mapped_column(String(255), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    snapshots: Mapped[list[PriceSnapshot]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def slots(self) -> int:
        return max(1, self.width * self.height)

    def __repr__(self) -> str:
        return f"<Item {self.id} {self.name!r}>"


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    # Headline flea price at capture time (roubles).
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    avg_24h: Mapped[int | None] = mapped_column(Integer)
    low_24h: Mapped[int | None] = mapped_column(Integer)
    high_24h: Mapped[int | None] = mapped_column(Integer)
    change_48h_percent: Mapped[float | None] = mapped_column(Float)
    # Best trader sell price observed at capture time, for historical margins.
    trader_price: Mapped[int | None] = mapped_column(Integer)
    trader_name: Mapped[str | None] = mapped_column(String(64))
    # "api" for scraped data, "ocr" for prices read off your own game client.
    source: Mapped[str] = mapped_column(String(16), default="api", nullable=False)

    item: Mapped[Item] = relationship(back_populates="snapshots")

    __table_args__ = (
        Index("ix_snapshot_item_time", "item_id", "recorded_at"),
        Index("ix_snapshot_time", "recorded_at"),
        # One snapshot per item per source per instant — makes re-running a
        # fetch idempotent if the clock hasn't moved.
        UniqueConstraint(
            "item_id", "recorded_at", "source", name="uq_snapshot_item_time_source"
        ),
    )

    def __repr__(self) -> str:
        return f"<PriceSnapshot {self.item_id} {self.price} @ {self.recorded_at:%F %T}>"
