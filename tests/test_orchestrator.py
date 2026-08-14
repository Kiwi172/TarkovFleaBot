"""State machine tests with fake vision and input.

The point of these: transition logic and the price-drift safety check are the
things most likely to lose you money, and they are exactly the things you
cannot conveniently test against a live client.
"""

from __future__ import annotations

import pytest

from flea_bot.decision.ranking import RankedItem
from flea_bot.input.backends import NullBackend
from flea_bot.input.controller import InputController
from flea_bot.orchestrator.machine import FleaBotMachine
from flea_bot.orchestrator.states import State, TradeIntent
from flea_bot.safety import RunGuard
from flea_bot.traders.prices import MarginResult
from flea_bot.vision.template import Match


class FakeMatcher:
    """Vision stub. `present` controls which templates are 'on screen'."""

    def __init__(self, present: set[str] | None = None):
        self.present = present if present is not None else set()
        self.calls: list[str] = []

    def _match(self, name: str) -> Match | None:
        self.calls.append(name)
        return Match(100, 200, 40, 20, 0.99) if name in self.present else None

    def find(self, template_name, **kwargs):
        return self._match(template_name)

    def exists(self, template_name, **kwargs):
        return self._match(template_name) is not None

    def wait_for(self, template_name, **kwargs):
        return self._match(template_name)

    def find_all(self, template_name, **kwargs):
        m = self._match(template_name)
        return [m] if m else []


class FakeReader:
    """OCR stub returning scripted values."""

    def __init__(self, price: int | None = 10_000, quantity: int = 1):
        self.price = price
        self.quantity = quantity

    def read_price(self, region="first_offer_price"):
        return self.price

    def read_quantity(self, region="first_offer_quantity"):
        return self.quantity

    def read_region(self, region, **kwargs):
        from flea_bot.vision.ocr import OCRResult

        return OCRResult(str(self.price), self.price, 95.0)

    def read_balance(self):
        return 1_000_000


def build_machine(
    config,
    *,
    present: set[str] | None = None,
    price: int | None = 10_000,
    verify: bool = False,
) -> tuple[FleaBotMachine, NullBackend]:
    config.input.min_action_delay = config.input.max_action_delay = 0
    config.input.post_click_delay = 0
    config.input.min_move_duration = config.input.max_move_duration = 0
    guard = RunGuard(config)
    backend = NullBackend()
    machine = FleaBotMachine(
        config,
        guard=guard,
        matcher=FakeMatcher(present),
        reader=FakeReader(price),
        controller=InputController(config, guard=guard, backend=backend),
        verify_states=verify,
    )
    return machine, backend


def ranked(name="Bottle of water", flea=10_000, trader=20_000) -> RankedItem:
    return RankedItem(
        margin=MarginResult(
            item_id=name.lower().replace(" ", "-"),
            item_name=name,
            flea_price=flea,
            trader_price=trader,
            trader="Therapist",
            price_source="api",
        )
    )


class TestTransitions:
    def test_starts_idle(self, config):
        machine, _ = build_machine(config)
        assert machine.state == State.IDLE

    def test_idle_to_flea_market(self, config):
        machine, _ = build_machine(config, present={"flea_market_tab"})
        assert machine.enter_flea_market()
        assert machine.state == State.IN_FLEA_MARKET

    def test_stays_idle_when_tab_not_found(self, config):
        machine, _ = build_machine(config, present=set())
        assert not machine.enter_flea_market()
        assert machine.state == State.IDLE

    def test_full_happy_path(self, config):
        machine, _ = build_machine(
            config, present={"flea_market_tab", "trader_tab", "confirm_button", "sell_button"}
        )
        machine.open_flea()
        assert machine.state == State.IN_FLEA_MARKET
        machine.select_item_trigger()
        assert machine.state == State.ITEM_SELECTED
        machine.begin_purchase()
        assert machine.state == State.CONFIRMING
        machine.confirmed()
        assert machine.state == State.IDLE
        machine.open_trader()
        assert machine.state == State.IN_TRADER_MENU
        machine.begin_sell()
        assert machine.state == State.SELLING
        machine.request_confirm()
        assert machine.state == State.CONFIRMING
        machine.confirmed()
        assert machine.state == State.IDLE

    def test_invalid_transition_raises(self, config):
        from transitions import MachineError

        machine, _ = build_machine(config)
        with pytest.raises(MachineError):
            machine.begin_sell()  # not valid from IDLE

    def test_reset_works_from_any_state(self, config):
        machine, _ = build_machine(config)
        machine.open_flea()
        machine.select_item_trigger()
        machine.reset()
        assert machine.state == State.IDLE

    def test_fail_moves_to_error_and_recovers(self, config):
        machine, _ = build_machine(config)
        machine.open_flea()
        machine.fail()
        assert machine.state == State.ERROR
        machine.recover()
        assert machine.state == State.IDLE

    def test_stop_is_terminal(self, config):
        machine, _ = build_machine(config)
        machine.stop()
        assert machine.state == State.STOPPED


class TestPriceDriftGuard:
    """The check that stops the bot buying at a price it never approved."""

    def test_accepts_price_matching_the_plan(self, config):
        config.thresholds.min_margin = 0
        machine, _ = build_machine(
            config, present={"flea_market_tab", "offer_row"}, price=10_000
        )
        machine.open_flea()
        intent = TradeIntent("Bottle of water", "b", 10_000, 20_000)
        machine.context.current = intent
        assert machine.select_item(intent)
        assert machine.state == State.ITEM_SELECTED

    def test_rejects_price_above_drift_limit(self, config):
        config.thresholds.min_margin = 0
        machine, _ = build_machine(
            config, present={"flea_market_tab", "offer_row"}, price=15_000
        )
        machine.max_price_drift = 0.10
        machine.open_flea()
        intent = TradeIntent("Bottle of water", "b", 10_000, 20_000)
        machine.context.current = intent
        assert not machine.select_item(intent)
        assert machine.state == State.IN_FLEA_MARKET, "must not advance"
        assert machine.context.skipped[0][1] == "price drift"

    def test_accepts_price_below_plan(self, config):
        """Cheaper than expected is good news, not drift."""
        config.thresholds.min_margin = 0
        machine, _ = build_machine(
            config, present={"flea_market_tab", "offer_row"}, price=5_000
        )
        machine.open_flea()
        intent = TradeIntent("Bottle of water", "b", 10_000, 20_000)
        machine.context.current = intent
        assert machine.select_item(intent)

    def test_rejects_when_margin_collapses(self, config):
        config.thresholds.min_margin = 8_000
        machine, _ = build_machine(
            config, present={"flea_market_tab", "offer_row"}, price=10_400
        )
        machine.max_price_drift = 0.50  # drift alone would allow this
        machine.open_flea()
        intent = TradeIntent("Bottle of water", "b", 10_000, 15_000)
        machine.context.current = intent
        assert not machine.select_item(intent)
        assert "margin collapsed" in machine.context.skipped[0][1]

    def test_rejects_unreadable_price(self, config):
        machine, _ = build_machine(
            config, present={"flea_market_tab", "offer_row"}, price=None
        )
        machine.open_flea()
        intent = TradeIntent("Bottle of water", "b", 10_000, 20_000)
        machine.context.current = intent
        assert not machine.select_item(intent)
        assert machine.context.skipped[0][1] == "price unreadable"

    def test_no_offer_row_fails_cleanly(self, config):
        machine, _ = build_machine(config, present={"flea_market_tab"})
        machine.open_flea()
        intent = TradeIntent("Nonexistent", "n", 10_000, 20_000)
        machine.context.current = intent
        assert not machine.select_item(intent)
        assert machine.state == State.IN_FLEA_MARKET


class TestTradeIntent:
    def test_drift_ratio(self):
        intent = TradeIntent("X", "x", 10_000, 20_000)
        intent.observed_price = 11_000
        assert intent.price_drift_ratio() == pytest.approx(0.10)

    def test_drift_none_before_observation(self):
        assert TradeIntent("X", "x", 10_000, 20_000).price_drift_ratio() is None

    def test_actual_margin_uses_observed_price(self):
        intent = TradeIntent("X", "x", 10_000, 20_000)
        assert intent.expected_margin == 10_000
        intent.observed_price = 12_000
        assert intent.actual_margin() == 8_000

    def test_from_ranked(self):
        intent = TradeIntent.from_ranked(ranked())
        assert intent.item_name == "Bottle of water"
        assert intent.expected_margin == 10_000


class TestRun:
    def test_queue_trades(self, config):
        machine, _ = build_machine(config)
        assert machine.queue_trades([ranked("A"), ranked("B")]) == 2
        assert len(machine.context.queue) == 2

    def test_run_completes_the_happy_path(self, config):
        config.thresholds.min_margin = 0
        machine, _ = build_machine(
            config,
            present={
                "flea_market_tab", "offer_row", "confirm_button",
                "trader_tab", "sell_button",
            },
            price=10_000,
        )
        machine.queue_trades([ranked()])
        context = machine.run()

        assert len(context.completed) == 1
        assert context.realised_profit == 10_000
        assert context.queue == []

    def test_run_skips_and_continues(self, config):
        """One bad trade must not abort the whole queue."""
        config.thresholds.min_margin = 0
        machine, _ = build_machine(
            config,
            present={
                "flea_market_tab", "offer_row", "confirm_button",
                "trader_tab", "sell_button",
            },
            price=99_999,  # far above plan -> drift rejection for both
        )
        machine.queue_trades([ranked("A"), ranked("B")])
        context = machine.run()

        assert len(context.completed) == 0
        assert len(context.skipped) == 2
        assert context.queue == []

    def test_kill_switch_halts_the_run(self, config):
        config.thresholds.min_margin = 0
        machine, _ = build_machine(
            config,
            present={
                "flea_market_tab", "offer_row", "confirm_button",
                "trader_tab", "sell_button",
            },
        )
        machine.queue_trades([ranked(f"Item{i}") for i in range(10)])
        machine.guard.kill()
        context = machine.run()

        assert machine.state == State.STOPPED
        assert len(context.completed) == 0
        assert context.queue, "the remaining queue must be preserved"

    def test_max_trades_caps_the_run(self, config):
        config.thresholds.min_margin = 0
        machine, _ = build_machine(
            config,
            present={
                "flea_market_tab", "offer_row", "confirm_button",
                "trader_tab", "sell_button",
            },
        )
        machine.queue_trades([ranked(f"Item{i}") for i in range(5)])
        context = machine.run(max_trades=2)
        assert len(context.completed) == 2
        assert len(context.queue) == 3

    def test_summary_shape(self, config):
        machine, _ = build_machine(config)
        assert set(machine.context.summary()) == {
            "completed", "skipped", "remaining", "errors", "realised_profit",
        }


class TestSessionBudget:
    """The spend cap must bound a run without corrupting it."""

    ALL_TEMPLATES = {
        "flea_market_tab", "offer_row", "confirm_button", "trader_tab", "sell_button",
    }

    def _machine(self, config, budget, price=10_000):
        config.thresholds.min_margin = 0
        config.safety.max_spend_per_session = budget
        return build_machine(config, present=self.ALL_TEMPLATES, price=price)[0]

    def test_run_stops_when_budget_exhausted(self, config):
        # Budget covers exactly 3 buys at 10k.
        machine = self._machine(config, budget=30_000)
        machine.queue_trades([ranked(f"Item{i}") for i in range(10)])
        context = machine.run()

        assert len(context.completed) == 3
        assert machine.guard.snapshot().spent == 30_000

    def test_unaffordable_trade_is_returned_to_the_queue(self, config):
        """A trade refused on budget must not be silently lost."""
        machine = self._machine(config, budget=25_000)
        machine.queue_trades([ranked(f"Item{i}") for i in range(5)])
        context = machine.run()

        assert len(context.completed) == 2
        # The 3rd was requeued, not skipped, so a bigger budget can reach it.
        assert len(context.queue) == 3
        assert context.queue[0].item_name == "Item2"

    def test_earnings_tracked_across_trades(self, config):
        machine = self._machine(config, budget=100_000)
        machine.queue_trades([ranked(f"Item{i}") for i in range(3)])
        machine.run()

        snap = machine.guard.snapshot()
        assert snap.spent == 30_000
        assert snap.earned == 60_000  # ranked() sells at 20k
        assert snap.net == 30_000
        assert (snap.purchases, snap.sales) == (3, 3)

    def test_zero_budget_blocks_everything(self, config):
        """max_spend_per_session=1 can't cover a 10k item."""
        machine = self._machine(config, budget=1)
        machine.queue_trades([ranked()])
        context = machine.run()

        assert context.completed == []
        assert machine.guard.snapshot().spent == 0

    def test_unlimited_budget_runs_the_whole_queue(self, config):
        machine = self._machine(config, budget=0)  # 0 = unlimited
        machine.queue_trades([ranked(f"Item{i}") for i in range(6)])
        context = machine.run()
        assert len(context.completed) == 6

    def test_budget_reserved_against_observed_not_planned_price(self, config):
        """A price that drifted up must consume the larger amount."""
        # Planned 10k, on screen 10.5k (within the 10% drift tolerance).
        machine = self._machine(config, budget=21_000, price=10_500)
        machine.queue_trades([ranked(f"Item{i}") for i in range(3)])
        context = machine.run()

        # 21,000 budget / 10,500 actual = exactly 2 buys, not 3.
        assert len(context.completed) == 2
        assert machine.guard.snapshot().spent == 21_000

    def test_failed_trade_releases_its_reservation(self, config):
        """An aborted purchase must not leak budget."""
        config.thresholds.min_margin = 0
        config.safety.max_spend_per_session = 100_000
        # No confirm_button -> the purchase never completes.
        machine, _ = build_machine(
            config,
            present={"flea_market_tab", "offer_row"},
            price=10_000,
        )
        machine.queue_trades([ranked()])
        machine.run()

        snap = machine.guard.snapshot()
        assert snap.spent == 0
        assert snap.committed == 0, "reservation must be released, not stranded"
        assert snap.remaining == 100_000


class TestSetupErrors:
    """Missing templates are a config problem; they must fail cleanly."""

    class ExplodingMatcher(FakeMatcher):
        def wait_for(self, template_name, **kwargs):
            raise KeyError(f"No template {template_name!r} in [window.templates]")

    def test_missing_template_key_does_not_raise(self, config):
        machine, _ = build_machine(config)
        machine.matcher = self.ExplodingMatcher()
        assert machine.enter_flea_market() is False
        assert machine.state == State.IDLE
        assert any("template" in e for e in machine.context.errors)

    def test_missing_template_file_does_not_raise(self, config):
        class MissingFile(FakeMatcher):
            def wait_for(self, template_name, **kwargs):
                raise FileNotFoundError("assets/templates/x.png")

        machine, _ = build_machine(config)
        machine.matcher = MissingFile()
        assert machine.enter_flea_market() is False

    def test_run_survives_missing_templates(self, config):
        """A missing asset must skip trades, not crash the run."""
        machine, _ = build_machine(config)
        machine.matcher = self.ExplodingMatcher()
        machine.queue_trades([ranked("A"), ranked("B")])
        context = machine.run()

        assert len(context.skipped) == 2
        assert context.completed == []
        assert context.queue == []


class TestStateVerification:
    def test_unverified_state_records_a_failure(self, config):
        """Entering a state whose template is absent must not pass silently."""
        machine, _ = build_machine(config, present=set(), verify=True)
        machine.open_flea()  # claims IN_FLEA_MARKET, but nothing is on screen
        assert machine.guard._consecutive_failures == 1

    def test_verified_state_resets_the_failure_streak(self, config):
        machine, _ = build_machine(config, present={"flea_market_tab"}, verify=True)
        machine.guard.record_failure("earlier")
        machine.open_flea()
        assert machine.guard._consecutive_failures == 0
