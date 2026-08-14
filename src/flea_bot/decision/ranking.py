"""Turn margins + price history into a ranked shortlist of trades.

The engine is deliberately transparent: every rejected item carries the reason
it was rejected, so when the shortlist comes back empty you can see which
threshold was responsible instead of guessing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from flea_bot.config import Config, get_config
from flea_bot.database.repository import PriceStats
from flea_bot.logging_setup import get_logger
from flea_bot.traders.prices import MarginResult

log = get_logger("decision")


class SortKey(str, Enum):
    """How to order the shortlist."""

    MARGIN = "margin"
    RATIO = "ratio"
    PER_SLOT = "per_slot"
    SCORE = "score"


class RejectReason(str, Enum):
    BELOW_MIN_MARGIN = "below_min_margin"
    BELOW_MIN_RATIO = "below_min_ratio"
    BELOW_MIN_PRICE = "below_min_price"
    ABOVE_MAX_PRICE = "above_max_price"
    TOO_VOLATILE = "too_volatile"
    NEGATIVE_MARGIN = "negative_margin"


@dataclass(frozen=True, slots=True)
class RankedItem:
    """A candidate trade that passed every filter."""

    margin: MarginResult
    stats: PriceStats | None = None
    score: float = 0.0

    @property
    def item_name(self) -> str:
        return self.margin.item_name

    @property
    def item_id(self) -> str:
        return self.margin.item_id

    @property
    def profit_margin(self) -> int:
        return self.margin.profit_margin

    @property
    def volatility(self) -> float | None:
        return self.stats.volatility if self.stats else None

    def as_dict(self) -> dict[str, Any]:
        out = self.margin.as_dict()
        out["score"] = round(self.score, 2)
        if self.stats:
            out["volatility"] = round(self.stats.volatility, 4)
            out["samples"] = self.stats.samples
            out["mean_price"] = round(self.stats.mean, 0)
        return out


@dataclass(frozen=True, slots=True)
class Rejection:
    item_name: str
    reason: RejectReason
    detail: str = ""


@dataclass(slots=True)
class RankingResult:
    """The shortlist plus an audit trail of what got filtered and why."""

    items: list[RankedItem] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    considered: int = 0

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def rejection_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.rejections:
            counts[r.reason.value] = counts.get(r.reason.value, 0) + 1
        return counts


def _score(margin: MarginResult, stats: PriceStats | None) -> float:
    """Blended desirability score.

    Profit-per-slot is the base (inventory space is the real constraint),
    scaled by return ratio so cheap high-multiple flips aren't buried under
    expensive low-multiple ones, then discounted by price volatility — a big
    margin on an item whose price swings wildly is likely to have evaporated
    by the time you act on it.
    """
    base = margin.profit_per_slot * (1.0 + margin.margin_ratio)
    if stats is not None and stats.is_reliable:
        base *= 1.0 / (1.0 + stats.volatility)
    return base


def rank_items(
    margins: Iterable[MarginResult],
    *,
    stats: Mapping[str, PriceStats] | None = None,
    config: Config | None = None,
    top_n: int | None = None,
    sort_by: SortKey = SortKey.SCORE,
    min_margin: int | None = None,
    min_margin_ratio: float | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    max_volatility: float | None = -1.0,
) -> RankingResult:
    """Filter and rank trades, most profitable first.

    Explicit keyword arguments override the corresponding config threshold.
    ``max_volatility`` uses a ``-1.0`` sentinel so that passing ``None``
    explicitly disables the volatility check.
    """
    cfg = config or get_config()
    th = cfg.thresholds

    lim_margin = th.min_margin if min_margin is None else min_margin
    lim_ratio = th.min_margin_ratio if min_margin_ratio is None else min_margin_ratio
    lim_min_price = th.min_flea_price if min_price is None else min_price
    lim_max_price = th.max_flea_price if max_price is None else max_price
    lim_vol = th.max_volatility if max_volatility == -1.0 else max_volatility
    limit = th.top_n if top_n is None else top_n

    stats = stats or {}
    result = RankingResult()

    for margin in margins:
        result.considered += 1
        name = margin.item_name
        item_stats = stats.get(margin.item_id)

        if margin.profit_margin <= 0:
            result.rejections.append(
                Rejection(name, RejectReason.NEGATIVE_MARGIN, f"{margin.profit_margin}")
            )
            continue
        if margin.flea_price < lim_min_price:
            result.rejections.append(
                Rejection(
                    name,
                    RejectReason.BELOW_MIN_PRICE,
                    f"{margin.flea_price} < {lim_min_price}",
                )
            )
            continue
        if margin.flea_price > lim_max_price:
            result.rejections.append(
                Rejection(
                    name,
                    RejectReason.ABOVE_MAX_PRICE,
                    f"{margin.flea_price} > {lim_max_price}",
                )
            )
            continue
        if margin.profit_margin < lim_margin:
            result.rejections.append(
                Rejection(
                    name,
                    RejectReason.BELOW_MIN_MARGIN,
                    f"{margin.profit_margin} < {lim_margin}",
                )
            )
            continue
        if margin.margin_ratio < lim_ratio:
            result.rejections.append(
                Rejection(
                    name,
                    RejectReason.BELOW_MIN_RATIO,
                    f"{margin.margin_ratio:.3f} < {lim_ratio:.3f}",
                )
            )
            continue
        # Only trust volatility once there are enough samples to mean anything.
        if (
            lim_vol is not None
            and item_stats is not None
            and item_stats.is_reliable
            and item_stats.volatility > lim_vol
        ):
            result.rejections.append(
                Rejection(
                    name,
                    RejectReason.TOO_VOLATILE,
                    f"{item_stats.volatility:.3f} > {lim_vol:.3f}",
                )
            )
            continue

        result.items.append(
            RankedItem(margin=margin, stats=item_stats, score=_score(margin, item_stats))
        )

    sort_funcs = {
        SortKey.MARGIN: lambda r: r.margin.profit_margin,
        SortKey.RATIO: lambda r: r.margin.margin_ratio,
        SortKey.PER_SLOT: lambda r: r.margin.profit_per_slot,
        SortKey.SCORE: lambda r: r.score,
    }
    result.items.sort(key=sort_funcs[sort_by], reverse=True)
    result.items = result.items[:limit]

    log.info(
        "Ranked {} candidate(s) from {} considered (top_n={}, sort={}). Rejections: {}",
        len(result.items),
        result.considered,
        limit,
        sort_by.value,
        result.rejection_summary() or "none",
    )
    return result


def top_profitable(
    margins: Iterable[MarginResult],
    n: int = 10,
    **kwargs: Any,
) -> list[RankedItem]:
    """Convenience wrapper returning just the ranked list."""
    return rank_items(margins, top_n=n, **kwargs).items
