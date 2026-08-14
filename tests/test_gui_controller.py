"""GUI controller: threading, events, and the budget/dry-run wiring.

No display required — the controller deliberately holds no widget references,
so all of this runs headless.
"""

from __future__ import annotations

import time

import pytest

from flea_bot.gui.controller import BotController, BotState, EventType


def wait_for(predicate, timeout=10.0, interval=0.02):
    """Poll until predicate() is truthy. Returns False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def controller(config, tmp_path, monkeypatch):
    """Controller wired to a stub price source so no network is touched."""
    from tests.conftest import make_item

    config.general.data_dir = tmp_path / "data"
    config.thresholds.min_margin = 0
    config.thresholds.min_margin_ratio = 0
    config.thresholds.min_flea_price = 0
    config.safety.max_spend_per_session = 100_000

    items = [make_item(f"Item{i}", 10_000, 20_000) for i in range(5)]
    monkeypatch.setattr(
        "flea_bot.scraper.base.fetch_with_fallback", lambda cfg: (items, "test")
    )
    return BotController(config)


class TestEventQueue:
    def test_drain_returns_and_clears(self, controller):
        controller.emit(EventType.LOG, "one")
        controller.emit(EventType.LOG, "two")
        assert [e.message for e in controller.drain()] == ["one", "two"]
        assert controller.drain() == []

    def test_drain_is_bounded(self, controller):
        for i in range(50):
            controller.emit(EventType.LOG, str(i))
        assert len(controller.drain(limit=10)) == 10
        assert len(controller.drain()) == 40

    def test_drain_never_blocks_when_empty(self, controller):
        start = time.monotonic()
        controller.drain()
        assert time.monotonic() - start < 0.1

    def test_state_change_emits_event(self, controller):
        controller._set_state(BotState.RUNNING)
        events = controller.drain()
        assert any(
            e.type is EventType.STATUS and e.payload is BotState.RUNNING for e in events
        )


class TestRunLifecycle:
    def test_run_completes_and_reports(self, controller):
        assert controller.start(budget=100_000, dry_run=True, top_n=5)
        assert wait_for(lambda: not controller.running), "worker should finish"

        kinds = {e.type for e in controller.drain()}
        assert EventType.FINISHED in kinds
        assert EventType.LEDGER in kinds

    def test_second_start_refused_while_running(self, controller):
        controller.start(budget=100_000, dry_run=True, top_n=5)
        # Whether or not it's still going, a second start must never launch a
        # concurrent worker.
        if controller.running:
            assert controller.start(budget=1, dry_run=True, top_n=1) is False
        controller.join(timeout=10)

    def test_dry_run_flag_reaches_config(self, controller):
        controller.start(budget=10_000, dry_run=True, top_n=1)
        assert controller.config.general.dry_run is True
        controller.join(timeout=10)

    def test_live_flag_reaches_config(self, controller, monkeypatch):
        # Force a no-op backend: a test must never be one missing template away
        # from driving the real mouse.
        from flea_bot.input import backends

        monkeypatch.setattr(backends, "get_backend", lambda **kw: backends.NullBackend())

        controller.start(budget=10_000, dry_run=False, top_n=1)
        assert controller.config.general.dry_run is False
        controller.join(timeout=10)

    def test_error_is_reported_not_raised(self, controller, monkeypatch):
        """A failure in the worker must surface as an ERROR event."""
        monkeypatch.setattr(
            "flea_bot.scraper.base.fetch_with_fallback",
            lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        controller.start(budget=1_000, dry_run=True, top_n=1)
        assert wait_for(lambda: not controller.running)

        errors = [e for e in controller.drain() if e.type is EventType.ERROR]
        assert errors and "boom" in errors[0].message
        assert controller.state is BotState.ERROR

    def test_no_candidates_finishes_cleanly(self, controller):
        """An empty shortlist is a normal finish, not an error."""
        controller.config.thresholds.min_margin = 999_999_999
        controller.start(budget=100_000, dry_run=True, top_n=5)
        assert wait_for(lambda: not controller.running)

        events = controller.drain()
        assert any(e.type is EventType.FINISHED for e in events)
        assert not any(e.type is EventType.ERROR for e in events)


class TestBudgetIntegration:
    def test_budget_bounds_the_run(self, controller):
        """25k budget against 10k items = 2 buys, regardless of queue length."""
        controller.start(budget=25_000, dry_run=True, top_n=5)
        assert wait_for(lambda: not controller.running)

        ledgers = [e.payload for e in controller.drain() if e.type is EventType.LEDGER]
        assert ledgers, "should have emitted ledger updates"
        assert ledgers[-1].spent <= 25_000, "must never exceed the cap"

    def test_ledger_updates_arrive_during_the_run(self, controller):
        """Counters must move progressively, not only at the end."""
        controller.start(budget=100_000, dry_run=True, top_n=5)
        assert wait_for(lambda: not controller.running)

        ledgers = [e.payload for e in controller.drain() if e.type is EventType.LEDGER]
        assert len(ledgers) >= 2, "expected per-trade ledger events"

    def test_unlimited_budget_accepted(self, controller):
        controller.start(budget=None, dry_run=True, top_n=2)
        assert wait_for(lambda: not controller.running)
        assert not any(e.type is EventType.ERROR for e in controller.drain())


class TestControls:
    def test_stop_before_start_is_safe(self, controller):
        controller.stop()  # no guard yet — must not raise

    def test_pause_before_start_is_safe(self, controller):
        controller.pause()

    def test_snapshot_none_before_start(self, controller):
        assert controller.snapshot() is None

    def test_stop_halts_the_run(self, controller):
        controller.start(budget=1_000_000, dry_run=True, top_n=50)
        # Let it get going, then kill it.
        wait_for(lambda: controller.state is BotState.RUNNING, timeout=5)
        controller.stop()
        assert wait_for(lambda: not controller.running, timeout=10)
