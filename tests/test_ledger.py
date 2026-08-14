"""Session ledger and the spend cap.

This is the money-safety layer, so the tests are deliberately paranoid about
the failure directions that cost roubles: overspending, double-spending via
concurrent reservations, and leaked reservations from abandoned trades.
"""

from __future__ import annotations

import threading

import pytest

from flea_bot.ledger import BudgetExceeded, SessionLedger
from flea_bot.safety import RunGuard


class TestBudgetEnforcement:
    def test_reserve_within_budget(self):
        led = SessionLedger(budget=100_000)
        led.reserve(40_000)
        assert led.snapshot().remaining == 60_000

    def test_reserve_beyond_budget_raises(self):
        led = SessionLedger(budget=100_000)
        led.reserve(80_000)
        with pytest.raises(BudgetExceeded, match="only 20,000 left"):
            led.reserve(30_000)

    def test_exact_budget_is_allowed(self):
        led = SessionLedger(budget=100_000)
        led.reserve(100_000)  # must not raise on the boundary
        assert led.snapshot().remaining == 0

    def test_unlimited_budget_never_refuses(self):
        led = SessionLedger(budget=None)
        for _ in range(100):
            led.reserve(10_000_000)
        assert led.snapshot().remaining is None

    def test_committed_reduces_remaining_before_spending(self):
        """In-flight purchases must not be spendable twice."""
        led = SessionLedger(budget=100_000)
        led.reserve(60_000)
        # Nothing has actually been paid yet...
        assert led.snapshot().spent == 0
        # ...but it is no longer available.
        assert led.snapshot().remaining == 40_000
        assert not led.can_afford(50_000)

    def test_release_returns_budget(self):
        led = SessionLedger(budget=100_000)
        led.reserve(60_000)
        led.release(60_000)
        assert led.snapshot().remaining == 100_000
        assert led.snapshot().spent == 0

    def test_commit_moves_committed_to_spent(self):
        led = SessionLedger(budget=100_000)
        led.reserve(60_000)
        led.commit(60_000)
        snap = led.snapshot()
        assert snap.spent == 60_000
        assert snap.committed == 0
        assert snap.remaining == 40_000
        assert snap.purchases == 1

    def test_commit_records_actual_price_when_it_differs(self):
        """Reserved at the planned price, paid at the on-screen price."""
        led = SessionLedger(budget=100_000)
        led.reserve(50_000)
        led.commit(50_000, actual=47_500)
        snap = led.snapshot()
        assert snap.spent == 47_500
        assert snap.remaining == 52_500, "the unspent difference returns to budget"

    def test_negative_reserve_rejected(self):
        with pytest.raises(ValueError):
            SessionLedger(budget=100).reserve(-1)


class TestEarnings:
    def test_net_is_earned_minus_spent(self):
        led = SessionLedger(budget=None)
        led.reserve(30_000)
        led.commit(30_000)
        led.record_sale(50_000)
        snap = led.snapshot()
        assert snap.spent == 30_000
        assert snap.earned == 50_000
        assert snap.net == 20_000

    def test_net_is_negative_before_first_sale(self):
        led = SessionLedger(budget=None)
        led.reserve(30_000)
        led.commit(30_000)
        assert led.snapshot().net == -30_000

    def test_counts_tracked(self):
        led = SessionLedger(budget=None)
        for _ in range(3):
            led.reserve(1_000)
            led.commit(1_000)
        led.record_sale(5_000)
        snap = led.snapshot()
        assert (snap.purchases, snap.sales) == (3, 1)

    def test_budget_fraction_for_progress_bar(self):
        led = SessionLedger(budget=100_000)
        led.reserve(25_000)
        led.commit(25_000)
        assert led.snapshot().budget_used_fraction == pytest.approx(0.25)

    def test_budget_fraction_zero_when_unlimited(self):
        assert SessionLedger(budget=None).snapshot().budget_used_fraction == 0.0

    def test_profit_per_hour_suppressed_in_first_minute(self):
        """A 10s sample must not extrapolate to a headline number."""
        led = SessionLedger(budget=None)
        led.start()
        led.record_sale(100_000)
        assert led.snapshot().profit_per_hour == 0.0

    def test_reset_clears_everything(self):
        led = SessionLedger(budget=100_000)
        led.reserve(10_000)
        led.commit(10_000)
        led.record_sale(20_000)
        led.reset()
        snap = led.snapshot()
        assert (snap.spent, snap.earned, snap.committed) == (0, 0, 0)


class TestThreadSafety:
    def test_concurrent_reservations_cannot_overspend(self):
        """Ten threads racing on a budget that fits only five."""
        led = SessionLedger(budget=50_000)
        granted: list[int] = []
        lock = threading.Lock()

        def worker():
            try:
                led.reserve(10_000)
            except BudgetExceeded:
                return
            with lock:
                granted.append(10_000)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(granted) == 5, "exactly five reservations should fit"
        assert led.snapshot().remaining == 0

    def test_snapshot_is_immutable(self):
        led = SessionLedger(budget=100)
        snap = led.snapshot()
        with pytest.raises((AttributeError, TypeError)):
            snap.spent = 999  # type: ignore[misc]


class TestRunGuardIntegration:
    def test_config_budget_applied(self, config):
        config.safety.max_spend_per_session = 250_000
        guard = RunGuard(config)
        assert guard.snapshot().budget == 250_000

    def test_zero_config_budget_means_unlimited(self, config):
        config.safety.max_spend_per_session = 0
        guard = RunGuard(config)
        assert guard.snapshot().budget is None
        assert guard.can_afford(999_999_999)

    def test_explicit_budget_overrides_config(self, config):
        """The GUI passes a budget directly; it must win over config."""
        config.safety.max_spend_per_session = 250_000
        guard = RunGuard(config, budget=10_000)
        assert guard.snapshot().budget == 10_000

    def test_reserve_returns_false_instead_of_raising(self, config):
        """Budget exhaustion is a normal end to a session, not an error."""
        config.safety.max_spend_per_session = 50_000
        guard = RunGuard(config)
        assert guard.reserve_spend(40_000) is True
        assert guard.reserve_spend(40_000) is False

    def test_budget_exhausted_flag(self, config):
        config.safety.max_spend_per_session = 50_000
        guard = RunGuard(config)
        assert not guard.budget_exhausted
        guard.reserve_spend(50_000)
        assert guard.budget_exhausted

    def test_unlimited_budget_never_exhausted(self, config):
        config.safety.max_spend_per_session = 0
        guard = RunGuard(config)
        guard.reserve_spend(10_000_000)
        assert not guard.budget_exhausted

    def test_full_buy_sell_cycle(self, config):
        config.safety.max_spend_per_session = 100_000
        guard = RunGuard(config)

        assert guard.reserve_spend(30_000)
        guard.commit_spend(30_000)
        guard.record_sale(45_000)

        snap = guard.snapshot()
        assert snap.spent == 30_000
        assert snap.earned == 45_000
        assert snap.net == 15_000
        assert snap.remaining == 70_000

    def test_abandoned_trade_releases_budget(self, config):
        """A reserved-then-failed purchase must not leak budget."""
        config.safety.max_spend_per_session = 100_000
        guard = RunGuard(config)

        guard.reserve_spend(60_000)
        guard.release_spend(60_000)  # trade aborted

        assert guard.snapshot().remaining == 100_000
        assert guard.reserve_spend(90_000), "released budget must be reusable"
