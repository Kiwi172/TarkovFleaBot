"""States, transitions, and the per-run context object."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from flea_bot.decision.ranking import RankedItem


class State(str, Enum):
    """Where the bot believes the UI is.

    ``believes`` is doing real work in that sentence — the state is an
    assumption that must be re-confirmed by vision on every transition, which
    is what :meth:`FleaBotMachine._verify_state` is for.
    """

    IDLE = "IDLE"
    IN_FLEA_MARKET = "IN_FLEA_MARKET"
    ITEM_SELECTED = "ITEM_SELECTED"
    SELLING = "SELLING"
    IN_TRADER_MENU = "IN_TRADER_MENU"
    CONFIRMING = "CONFIRMING"
    # Terminal / recovery states, not in the original spec but the FSM is
    # unusable without somewhere to land when vision desyncs.
    ERROR = "ERROR"
    STOPPED = "STOPPED"


class Trigger(str, Enum):
    """Named transitions. Use these rather than raw strings at call sites."""

    OPEN_FLEA = "open_flea"
    SELECT_ITEM = "select_item"
    BEGIN_PURCHASE = "begin_purchase"
    OPEN_TRADER = "open_trader"
    BEGIN_SELL = "begin_sell"
    REQUEST_CONFIRM = "request_confirm"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    BACK_TO_FLEA = "back_to_flea"
    RESET = "reset"
    FAIL = "fail"
    RECOVER = "recover"
    STOP = "stop"


# (trigger, source(s), destination). Kept as data so the graph is readable in
# one place and can be rendered without instantiating the machine.
TRANSITIONS: list[tuple[str, str | list[str], str]] = [
    (Trigger.OPEN_FLEA, [State.IDLE, State.IN_TRADER_MENU], State.IN_FLEA_MARKET),
    (Trigger.SELECT_ITEM, State.IN_FLEA_MARKET, State.ITEM_SELECTED),
    (Trigger.BEGIN_PURCHASE, State.ITEM_SELECTED, State.CONFIRMING),
    (Trigger.OPEN_TRADER, [State.IDLE, State.IN_FLEA_MARKET], State.IN_TRADER_MENU),
    (Trigger.BEGIN_SELL, State.IN_TRADER_MENU, State.SELLING),
    (Trigger.REQUEST_CONFIRM, State.SELLING, State.CONFIRMING),
    (Trigger.CONFIRMED, State.CONFIRMING, State.IDLE),
    (Trigger.CANCELLED, State.CONFIRMING, State.IDLE),
    (Trigger.BACK_TO_FLEA, [State.ITEM_SELECTED, State.CONFIRMING], State.IN_FLEA_MARKET),
    (Trigger.RESET, "*", State.IDLE),
    (Trigger.FAIL, "*", State.ERROR),
    (Trigger.RECOVER, State.ERROR, State.IDLE),
    (Trigger.STOP, "*", State.STOPPED),
]


@dataclass
class TradeIntent:
    """One planned buy-then-sell, carried through the state machine."""

    item_name: str
    item_id: str
    expected_flea_price: int
    expected_trader_price: int
    trader: str | None = None
    quantity: int = 1
    # Filled in by OCR once the bot actually reads the listing on screen.
    observed_price: int | None = None
    observed_quantity: int | None = None

    @classmethod
    def from_ranked(cls, ranked: RankedItem) -> TradeIntent:
        m = ranked.margin
        return cls(
            item_name=m.item_name,
            item_id=m.item_id,
            expected_flea_price=m.flea_price,
            expected_trader_price=m.trader_price,
            trader=m.trader,
            quantity=m.quantity,
        )

    @property
    def expected_margin(self) -> int:
        return self.expected_trader_price - self.expected_flea_price

    def actual_margin(self) -> int | None:
        """Margin using the price actually observed on screen."""
        if self.observed_price is None:
            return None
        return self.expected_trader_price - self.observed_price

    def price_drift_ratio(self) -> float | None:
        """How far the on-screen price moved from what we planned for.

        The single most important safety number in the whole bot: if the flea
        price on screen is materially higher than the API said, the trade is
        not the trade we vetted and must not be executed.
        """
        if self.observed_price is None or self.expected_flea_price <= 0:
            return None
        return (self.observed_price - self.expected_flea_price) / self.expected_flea_price


@dataclass
class RunContext:
    """Mutable bookkeeping for one orchestrator run."""

    queue: list[TradeIntent] = field(default_factory=list)
    current: TradeIntent | None = None
    completed: list[TradeIntent] = field(default_factory=list)
    skipped: list[tuple[TradeIntent, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def next_intent(self) -> TradeIntent | None:
        self.current = self.queue.pop(0) if self.queue else None
        return self.current

    def complete_current(self) -> None:
        if self.current is not None:
            self.completed.append(self.current)
            self.current = None

    def skip_current(self, reason: str) -> None:
        if self.current is not None:
            self.skipped.append((self.current, reason))
            self.current = None

    @property
    def realised_profit(self) -> int:
        return sum(
            t.actual_margin() or t.expected_margin for t in self.completed
        )

    def summary(self) -> dict[str, object]:
        return {
            "completed": len(self.completed),
            "skipped": len(self.skipped),
            "remaining": len(self.queue),
            "errors": len(self.errors),
            "realised_profit": self.realised_profit,
        }
