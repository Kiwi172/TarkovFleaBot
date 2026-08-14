"""Session ledger: what the bot has spent, what it has earned, what's left.

Kept separate from :class:`~flea_bot.safety.RunGuard` because two different
things need it for two different reasons: the guard enforces the budget as a
hard stop, and the GUI reads it to draw live counters. Both touch it from
different threads, so every mutation is under a lock and every read returns an
immutable :class:`LedgerSnapshot`.

The budget is a **pre-commitment**, not a post-hoc tally. ``reserve`` is called
*before* money is spent and refuses anything that would breach the cap; a
purchase that fails afterwards is released. Checking after the fact would mean
the cap is only ever discovered as already-broken.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class LedgerSnapshot:
    """An immutable read of the ledger, safe to hand to another thread."""

    spent: int = 0
    earned: int = 0
    committed: int = 0
    budget: int | None = None
    purchases: int = 0
    sales: int = 0
    started_at: datetime | None = None

    @property
    def net(self) -> int:
        """Profit so far. Negative until the first sale completes."""
        return self.earned - self.spent

    @property
    def remaining(self) -> int | None:
        """Spendable roubles left, or None when unlimited.

        Subtracts money reserved for in-flight purchases as well as money
        already spent, so a second trade can't be started against roubles the
        first one has already claimed.
        """
        if self.budget is None:
            return None
        return max(0, self.budget - self.spent - self.committed)

    @property
    def budget_used_fraction(self) -> float:
        """0.0-1.0 for a progress bar. 0.0 when the budget is unlimited."""
        if not self.budget:
            return 0.0
        return min(1.0, (self.spent + self.committed) / self.budget)

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    @property
    def profit_per_hour(self) -> float:
        """Extrapolated hourly rate. 0.0 until a minute has elapsed.

        The guard against short windows matters: thirty seconds in, a single
        sale extrapolates to a wildly misleading number.
        """
        seconds = self.elapsed_seconds
        if seconds < 60:
            return 0.0
        return self.net / (seconds / 3600.0)


class BudgetExceeded(RuntimeError):
    """A purchase was refused because it would breach the session budget."""


@dataclass
class SessionLedger:
    """Thread-safe running total for one bot session."""

    budget: int | None = None
    _spent: int = 0
    _earned: int = 0
    _committed: int = 0
    _purchases: int = 0
    _sales: int = 0
    _started_at: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self) -> None:
        with self._lock:
            self._started_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    def can_afford(self, amount: int) -> bool:
        """Would spending ``amount`` stay within budget? Does not reserve."""
        with self._lock:
            return self._can_afford_locked(amount)

    def _can_afford_locked(self, amount: int) -> bool:
        if self.budget is None:
            return True
        return self._spent + self._committed + amount <= self.budget

    def reserve(self, amount: int) -> None:
        """Claim ``amount`` against the budget before spending it.

        Raises :class:`BudgetExceeded` if it doesn't fit. Pair every successful
        reserve with exactly one :meth:`commit` or :meth:`release`.
        """
        if amount < 0:
            raise ValueError("Cannot reserve a negative amount")
        with self._lock:
            if not self._can_afford_locked(amount):
                remaining = max(0, (self.budget or 0) - self._spent - self._committed)
                raise BudgetExceeded(
                    f"Purchase of {amount:,} would exceed the session budget "
                    f"({self.budget:,}); only {remaining:,} left."
                )
            self._committed += amount

    def commit(self, reserved: int, actual: int | None = None) -> None:
        """Convert a reservation into real spend.

        ``actual`` covers the case where the price on screen differed from the
        price we reserved against — the ledger records what was really paid.
        """
        paid = reserved if actual is None else actual
        with self._lock:
            self._committed = max(0, self._committed - reserved)
            self._spent += paid
            self._purchases += 1

    def release(self, reserved: int) -> None:
        """Give back a reservation for a purchase that didn't happen."""
        with self._lock:
            self._committed = max(0, self._committed - reserved)

    def record_sale(self, amount: int) -> None:
        with self._lock:
            self._earned += amount
            self._sales += 1

    # ------------------------------------------------------------------
    def snapshot(self) -> LedgerSnapshot:
        """Consistent point-in-time read. This is what the GUI polls."""
        with self._lock:
            return LedgerSnapshot(
                spent=self._spent,
                earned=self._earned,
                committed=self._committed,
                budget=self.budget,
                purchases=self._purchases,
                sales=self._sales,
                started_at=self._started_at,
            )

    def reset(self) -> None:
        with self._lock:
            self._spent = 0
            self._earned = 0
            self._committed = 0
            self._purchases = 0
            self._sales = 0
            self._started_at = None
