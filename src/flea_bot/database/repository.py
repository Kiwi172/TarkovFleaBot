"""Session management, snapshot inserts, and rolling-window statistics."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from flea_bot.config import Config, get_config
from flea_bot.database.models import Base, Item, PriceSnapshot, utcnow
from flea_bot.logging_setup import get_logger
from flea_bot.scraper.models import ItemPrice

log = get_logger("database")


@dataclass(frozen=True, slots=True)
class PriceStats:
    """Rolling statistics for one item over a time window."""

    item_id: str
    item_name: str
    samples: int
    mean: float
    minimum: int
    maximum: int
    latest: int
    stddev: float
    window_hours: int

    @property
    def volatility(self) -> float:
        """Coefficient of variation (stddev / mean).

        Scale-free, so a 50k item and a 500k item are directly comparable.
        Returns 0.0 when there's no meaningful spread.
        """
        return self.stddev / self.mean if self.mean > 0 else 0.0

    @property
    def spread(self) -> int:
        return self.maximum - self.minimum

    @property
    def is_reliable(self) -> bool:
        """Two samples can't tell you anything about variance."""
        return self.samples >= 3


def _ensure_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; reattach UTC so maths is correct."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class PriceRepository:
    """All database access. Owns the engine and hands out scoped sessions."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        db_path: Path | str | None = None,
        echo: bool = False,
    ) -> None:
        self.config = config or get_config()
        if db_path is None:
            url = f"sqlite:///{self.config.db_path}"
        elif str(db_path) == ":memory:":
            url = "sqlite://"
        else:
            url = f"sqlite:///{db_path}"

        self.engine: Engine = create_engine(url, echo=echo, future=True)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.create_schema()

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope — commits on success, rolls back on error."""
        sess = self._session_factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def upsert_item(self, sess: Session, price: ItemPrice) -> Item:
        item = sess.get(Item, price.item_id)
        if item is None:
            item = Item(id=price.item_id, name=price.item_name)
            sess.add(item)
        item.name = price.item_name
        item.short_name = price.short_name
        item.base_price = price.base_price
        item.width = price.width
        item.height = price.height
        item.types = ",".join(price.types)
        item.last_seen = utcnow()
        return item

    def insert_snapshot(
        self,
        price: ItemPrice,
        *,
        recorded_at: datetime | None = None,
        source: str = "api",
        sess: Session | None = None,
    ) -> PriceSnapshot:
        """Record one price observation. Creates the item row if new."""
        if sess is not None:
            return self._insert_snapshot(sess, price, recorded_at, source)
        with self.session() as own:
            return self._insert_snapshot(own, price, recorded_at, source)

    def _insert_snapshot(
        self,
        sess: Session,
        price: ItemPrice,
        recorded_at: datetime | None,
        source: str,
    ) -> PriceSnapshot:
        self.upsert_item(sess, price)
        trader = price.best_trader()
        snapshot = PriceSnapshot(
            item_id=price.item_id,
            recorded_at=recorded_at or utcnow(),
            price=price.price,
            quantity=price.quantity,
            avg_24h=price.avg_24h,
            low_24h=price.low_24h,
            high_24h=price.high_24h,
            change_48h_percent=price.change_48h_percent,
            trader_price=trader.price_rub if trader else None,
            trader_name=trader.vendor if trader else None,
            source=source,
        )
        sess.add(snapshot)
        return snapshot

    def insert_snapshots(
        self,
        prices: Iterable[ItemPrice],
        *,
        recorded_at: datetime | None = None,
        source: str = "api",
    ) -> int:
        """Bulk-insert a whole fetch in one transaction.

        All rows share a timestamp so they form a coherent snapshot of the
        market at one instant rather than smearing across the fetch duration.
        """
        stamp = recorded_at or utcnow()
        count = 0
        with self.session() as sess:
            for price in prices:
                self._insert_snapshot(sess, price, stamp, source)
                count += 1
        log.info("Inserted {} price snapshot(s) at {:%F %T}", count, stamp)
        return count

    def prune_older_than(self, days: int) -> int:
        """Delete snapshots older than ``days``. Returns rows removed."""
        cutoff = utcnow() - timedelta(days=days)
        with self.session() as sess:
            result = sess.execute(
                delete(PriceSnapshot).where(PriceSnapshot.recorded_at < cutoff)
            )
            removed = result.rowcount or 0
        log.info("Pruned {} snapshot(s) older than {} day(s)", removed, days)
        return removed

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def latest_snapshot(self, item_id: str) -> PriceSnapshot | None:
        with self.session() as sess:
            return sess.scalars(
                select(PriceSnapshot)
                .where(PriceSnapshot.item_id == item_id)
                .order_by(PriceSnapshot.recorded_at.desc())
                .limit(1)
            ).first()

    def history(
        self,
        item_id: str,
        *,
        window_hours: int | None = None,
    ) -> Sequence[PriceSnapshot]:
        """Snapshots for one item, oldest first."""
        stmt = select(PriceSnapshot).where(PriceSnapshot.item_id == item_id)
        if window_hours is not None:
            stmt = stmt.where(
                PriceSnapshot.recorded_at >= utcnow() - timedelta(hours=window_hours)
            )
        stmt = stmt.order_by(PriceSnapshot.recorded_at.asc())
        with self.session() as sess:
            return list(sess.scalars(stmt))

    def stats(
        self,
        item_id: str,
        *,
        window_hours: int | None = None,
        source: str | None = None,
    ) -> PriceStats | None:
        """Rolling mean/volatility for one item. None if there's no data.

        Computes variance from SUM(x) and SUM(x*x) in a single aggregate query
        — SQLite has no STDDEV, and pulling every row into Python would be
        wasteful once history gets long.

        ``source`` restricts the window to one origin. Pass it whenever the
        database may hold more than one: SPT and live-Tarkov prices describe
        different economies, and averaging them produces a number that
        describes neither.
        """
        hours = window_hours or self.config.thresholds.history_window_hours
        cutoff = utcnow() - timedelta(hours=hours)

        with self.session() as sess:
            stmt = (
                select(
                    func.count(PriceSnapshot.id),
                    func.avg(PriceSnapshot.price),
                    func.min(PriceSnapshot.price),
                    func.max(PriceSnapshot.price),
                    func.sum(PriceSnapshot.price * PriceSnapshot.price),
                )
                .where(PriceSnapshot.item_id == item_id)
                .where(PriceSnapshot.recorded_at >= cutoff)
            )
            if source is not None:
                stmt = stmt.where(PriceSnapshot.source == source)
            row = sess.execute(stmt).one()

            count, mean, minimum, maximum, sum_squares = row
            if not count or mean is None:
                return None

            latest_stmt = (
                select(PriceSnapshot.price)
                .where(PriceSnapshot.item_id == item_id)
                .order_by(PriceSnapshot.recorded_at.desc())
                .limit(1)
            )
            if source is not None:
                latest_stmt = latest_stmt.where(PriceSnapshot.source == source)
            latest = sess.scalars(latest_stmt).first()

            name = sess.scalars(
                select(Item.name).where(Item.id == item_id)
            ).first() or item_id

        # Population variance: E[x^2] - E[x]^2. Clamp at 0 — float error can
        # push it a hair negative when every sample is identical.
        variance = max(0.0, (sum_squares / count) - (mean * mean))

        return PriceStats(
            item_id=item_id,
            item_name=name,
            samples=int(count),
            mean=float(mean),
            minimum=int(minimum),
            maximum=int(maximum),
            latest=int(latest if latest is not None else mean),
            stddev=math.sqrt(variance),
            window_hours=hours,
        )

    def stats_for_all(
        self,
        *,
        window_hours: int | None = None,
        source: str | None = None,
    ) -> dict[str, PriceStats]:
        """Rolling stats for every item with data in the window, in one query.

        Pass ``source`` to keep economies separate — see :meth:`stats`.
        """
        hours = window_hours or self.config.thresholds.history_window_hours
        cutoff = utcnow() - timedelta(hours=hours)

        with self.session() as sess:
            stmt = (
                select(
                    PriceSnapshot.item_id,
                    Item.name,
                    func.count(PriceSnapshot.id),
                    func.avg(PriceSnapshot.price),
                    func.min(PriceSnapshot.price),
                    func.max(PriceSnapshot.price),
                    func.sum(PriceSnapshot.price * PriceSnapshot.price),
                    func.max(PriceSnapshot.recorded_at),
                )
                .join(Item, Item.id == PriceSnapshot.item_id)
                .where(PriceSnapshot.recorded_at >= cutoff)
                .group_by(PriceSnapshot.item_id, Item.name)
            )
            latest_stmt = (
                select(PriceSnapshot.item_id, PriceSnapshot.price)
                .where(PriceSnapshot.recorded_at >= cutoff)
                .order_by(PriceSnapshot.recorded_at.asc())
            )
            if source is not None:
                stmt = stmt.where(PriceSnapshot.source == source)
                latest_stmt = latest_stmt.where(PriceSnapshot.source == source)

            rows = sess.execute(stmt).all()
            # One extra pass for latest price per item; cheap and clearer than
            # a correlated subquery in the aggregate above.
            latest_by_item = dict(sess.execute(latest_stmt).all())

        out: dict[str, PriceStats] = {}
        for item_id, name, count, mean, low, high, sum_sq, _last_at in rows:
            if not count or mean is None:
                continue
            variance = max(0.0, (sum_sq / count) - (mean * mean))
            out[item_id] = PriceStats(
                item_id=item_id,
                item_name=name,
                samples=int(count),
                mean=float(mean),
                minimum=int(low),
                maximum=int(high),
                latest=int(latest_by_item.get(item_id, mean)),
                stddev=math.sqrt(variance),
                window_hours=hours,
            )
        return out

    def item_count(self) -> int:
        with self.session() as sess:
            return sess.scalar(select(func.count(Item.id))) or 0

    def snapshot_count(self) -> int:
        with self.session() as sess:
            return sess.scalar(select(func.count(PriceSnapshot.id))) or 0
