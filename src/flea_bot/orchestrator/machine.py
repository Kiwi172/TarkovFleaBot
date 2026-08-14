"""The state machine that drives the bot.

Design rule: **transitions are proposed by logic, but only committed once
vision confirms them.** Every ``on_enter_*`` callback re-checks the screen for
the template that defines that state. If the check fails, the machine drops to
``ERROR`` rather than continuing to click against a UI it has lost track of.
That is the difference between a bot that stops and one that empties your
stash into a vendor by accident.

The machine is also fully driveable with fake vision/input (see
``tests/test_orchestrator.py``), which is how you test transition logic
without a running game.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from transitions import Machine, MachineError

from flea_bot.config import Config, get_config
from flea_bot.decision.ranking import RankedItem
from flea_bot.input.controller import InputController
from flea_bot.logging_setup import get_logger
from flea_bot.orchestrator.states import (
    TRANSITIONS,
    RunContext,
    State,
    TradeIntent,
    Trigger,
)
from flea_bot.safety import BotStopped, RunGuard, SafetyLimitExceeded
from flea_bot.vision.ocr import TextReader
from flea_bot.vision.template import TemplateMatcher

log = get_logger("orchestrator")

# Which template must be visible for the bot to believe it is in a given state.
STATE_TEMPLATES: dict[str, str] = {
    State.IN_FLEA_MARKET: "flea_market_tab",
    State.IN_TRADER_MENU: "trader_tab",
    State.CONFIRMING: "confirm_button",
}

# Refuse a trade if the on-screen price is this much above the planned price.
DEFAULT_MAX_PRICE_DRIFT = 0.10


class BudgetReached(RuntimeError):
    """The session spend cap can't cover the next trade.

    A normal, successful end to a run — distinct from :class:`BotStopped`
    (user intervention) and :class:`SafetyLimitExceeded` (something is wrong).
    """


class FleaBotMachine:
    """Vision-driven FSM over the flea market and trader screens."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        guard: RunGuard | None = None,
        matcher: TemplateMatcher | None = None,
        reader: TextReader | None = None,
        controller: InputController | None = None,
        max_price_drift: float = DEFAULT_MAX_PRICE_DRIFT,
        verify_states: bool = True,
    ) -> None:
        self.config = config or get_config()
        self.guard = guard or RunGuard(self.config)
        self.matcher = matcher or TemplateMatcher(self.config)
        self.reader = reader or TextReader(self.config)
        self.input = controller or InputController(self.config, guard=self.guard)
        self.context = RunContext()
        self.max_price_drift = max_price_drift
        # Disabled in unit tests, where there is no screen to look at.
        self.verify_states = verify_states

        self.machine = Machine(
            model=self,
            states=[s.value for s in State],
            initial=State.IDLE.value,
            transitions=[
                {
                    "trigger": t.value if isinstance(t, Trigger) else t,
                    "source": (
                        [s.value for s in src]
                        if isinstance(src, list)
                        else (src if src == "*" else src.value)
                    ),
                    "dest": dest.value,
                }
                for t, src, dest in TRANSITIONS
            ],
            auto_transitions=False,
            ignore_invalid_triggers=False,
            after_state_change="_after_state_change",
        )

    # ------------------------------------------------------------------
    # state verification
    # ------------------------------------------------------------------
    def _after_state_change(self, *_args: Any, **_kwargs: Any) -> None:
        log.info("State -> {}", self.state)
        if self.verify_states:
            self._verify_state()

    def _verify_state(self) -> None:
        """Confirm the screen actually shows the state we just moved into."""
        template = STATE_TEMPLATES.get(self.state)
        if template is None:
            return
        try:
            found = self.matcher.exists(template)
        except FileNotFoundError as exc:
            # A missing template asset is a setup problem, not a UI desync.
            log.error("{}", exc)
            self.context.errors.append(str(exc))
            found = False

        if found:
            self.guard.record_success()
        else:
            log.warning(
                "State {} expected template {!r} on screen but it wasn't found.",
                self.state,
                template,
            )
            self.guard.record_failure(f"state {self.state} unverified")

    # ------------------------------------------------------------------
    # low-level UI steps
    # ------------------------------------------------------------------
    def _click_template(self, template: str, *, timeout: float = 8.0) -> bool:
        """Wait for a UI element, then click its centre.

        An unconfigured template name (KeyError) or an uncaptured image file
        (FileNotFoundError) are both *setup* problems, not UI desyncs. They are
        reported as a clean failure rather than an exception so one missing
        asset doesn't produce a traceback per queued trade — run
        ``flea-bot doctor`` to see all of them at once.
        """
        try:
            match = self.matcher.wait_for(template, timeout=timeout)
        except (KeyError, FileNotFoundError) as exc:
            log.error("Template {!r} is not usable: {}", template, exc)
            self.context.errors.append(f"template {template!r}: {exc}")
            self.guard.record_failure(f"template {template!r} unusable")
            return False

        if match is None:
            self.guard.record_failure(f"template {template!r} never appeared")
            return False
        self.input.click(match.center)
        self.guard.record_success()
        return True

    def search_for(self, item_name: str) -> None:
        """Type an item name into the flea market search box."""
        box = self.config.window.region("search_box")
        centre = (box[0] + box[2] // 2, box[1] + box[3] // 2)
        self.input.type_into(centre, item_name)
        self.input.press("enter")
        # Let the offer list repopulate before anything reads it.
        self.input.pause(0.6)

    def read_first_offer(self) -> tuple[int | None, int]:
        """OCR the price and stack size of the top offer row."""
        price = self.reader.read_price("first_offer_price")
        quantity = self.reader.read_quantity("first_offer_quantity")
        return price, quantity

    # ------------------------------------------------------------------
    # high-level flow
    # ------------------------------------------------------------------
    def queue_trades(self, ranked: Iterable[RankedItem]) -> int:
        """Load the ranked shortlist into the run queue."""
        intents = [TradeIntent.from_ranked(r) for r in ranked]
        self.context.queue.extend(intents)
        log.info("Queued {} trade intent(s)", len(intents))
        return len(intents)

    def enter_flea_market(self) -> bool:
        """IDLE/IN_TRADER_MENU -> IN_FLEA_MARKET."""
        self.guard.checkpoint()
        if not self._click_template("flea_market_tab"):
            return False
        self.open_flea()  # type: ignore[attr-defined]
        return self.state == State.IN_FLEA_MARKET

    def select_item(self, intent: TradeIntent) -> bool:
        """IN_FLEA_MARKET -> ITEM_SELECTED, with a price sanity check.

        Aborts the trade if the on-screen price has drifted materially above
        the price the decision engine approved.
        """
        self.guard.checkpoint()
        self.search_for(intent.item_name)

        row = self.matcher.wait_for("offer_row", region="offer_list", timeout=6.0)
        if row is None:
            self.guard.record_failure(f"no offer row for {intent.item_name!r}")
            return False

        price, quantity = self.read_first_offer()
        intent.observed_price = price
        intent.observed_quantity = quantity

        if price is None:
            log.warning("Could not read a price for {!r} — skipping.", intent.item_name)
            self.context.skip_current("price unreadable")
            return False

        drift = intent.price_drift_ratio()
        if drift is not None and drift > self.max_price_drift:
            log.warning(
                "Skipping {!r}: on-screen price {} is {:.1%} above planned {} "
                "(limit {:.1%}).",
                intent.item_name,
                price,
                drift,
                intent.expected_flea_price,
                self.max_price_drift,
            )
            self.context.skip_current("price drift")
            return False

        margin = intent.actual_margin()
        if margin is not None and margin < self.config.thresholds.min_margin:
            log.warning(
                "Skipping {!r}: actual margin {} below minimum {}.",
                intent.item_name,
                margin,
                self.config.thresholds.min_margin,
            )
            self.context.skip_current("margin collapsed at execution time")
            return False

        self.input.click(row.center)
        self.select_item_trigger()
        return self.state == State.ITEM_SELECTED

    # `select_item` is taken by the method above, so the raw trigger gets an
    # explicit alias rather than shadowing it.
    def select_item_trigger(self) -> None:
        self.trigger(Trigger.SELECT_ITEM.value)  # type: ignore[attr-defined]

    def confirm_action(self, *, accept: bool = True) -> bool:
        """CONFIRMING -> IDLE via the confirm or cancel button."""
        self.guard.checkpoint()
        template = "confirm_button" if accept else "cancel_button"
        if not self._click_template(template, timeout=6.0):
            return False
        self.input.pause(0.5)
        if accept:
            self.confirmed()  # type: ignore[attr-defined]
        else:
            self.cancelled()  # type: ignore[attr-defined]
        return True

    def enter_trader_menu(self) -> bool:
        self.guard.checkpoint()
        if not self._click_template("trader_tab"):
            return False
        self.open_trader()  # type: ignore[attr-defined]
        return self.state == State.IN_TRADER_MENU

    def sell_to_trader(self, intent: TradeIntent) -> bool:
        """IN_TRADER_MENU -> SELLING -> CONFIRMING -> IDLE."""
        self.guard.checkpoint()
        self.begin_sell()  # type: ignore[attr-defined]

        if not self._click_template("sell_button", timeout=8.0):
            self.fail()  # type: ignore[attr-defined]
            return False

        self.request_confirm()  # type: ignore[attr-defined]
        return self.confirm_action(accept=True)

    # ------------------------------------------------------------------
    def run(self, *, max_trades: int | None = None) -> RunContext:
        """Work the queue until it's empty, stopped, or capped.

        Returns the :class:`RunContext` either way — a stopped run still
        reports what it managed to do.
        """
        log.info(
            "Starting run: {} queued, dry_run={}",
            len(self.context.queue),
            self.guard.dry_run,
        )
        processed = 0

        try:
            while self.context.queue:
                if max_trades is not None and processed >= max_trades:
                    log.info("Reached max_trades={} — stopping.", max_trades)
                    break

                self.guard.checkpoint()
                intent = self.context.next_intent()
                if intent is None:
                    break

                log.info(
                    "Trade {}: {!r} buy~{} sell~{} margin~{}",
                    processed + 1,
                    intent.item_name,
                    intent.expected_flea_price,
                    intent.expected_trader_price,
                    intent.expected_margin,
                )

                try:
                    self._execute_trade(intent)
                except (BotStopped, SafetyLimitExceeded, BudgetReached):
                    raise
                except Exception as exc:
                    log.exception("Trade failed: {}", exc)
                    self.context.errors.append(f"{intent.item_name}: {exc}")
                    self.context.skip_current(f"exception: {exc}")
                    self._recover()

                processed += 1

        except BotStopped:
            log.warning("Run stopped by kill switch.")
            self._safe_trigger(Trigger.STOP)
        except SafetyLimitExceeded as exc:
            log.error("Safety limit: {}", exc)
            self._safe_trigger(Trigger.STOP)
        except BudgetReached as exc:
            # Expected outcome, not a fault — the cap did its job.
            log.info("Session budget reached: {}", exc)

        snap = self.guard.snapshot()
        log.info(
            "Run summary: {} | spent {:,} earned {:,} net {:+,}",
            self.context.summary(),
            snap.spent,
            snap.earned,
            snap.net,
        )
        return self.context

    def _execute_trade(self, intent: TradeIntent) -> None:
        """One buy-then-sell cycle, bounded by the session spend cap.

        Budget handling is a reserve/commit/release triple. The reservation is
        taken against the *observed* price (after ``select_item`` has read it
        off screen), because that is the number that will actually leave the
        stash — reserving against the planned price would let a drifted price
        slip past the cap.
        """
        if self.state != State.IN_FLEA_MARKET and not self.enter_flea_market():
            self.context.skip_current("could not open flea market")
            return

        if not self.select_item(intent):
            # select_item already recorded the skip and its reason.
            if self.context.current is not None:
                self.context.skip_current("selection failed")
            self._back_to_flea()
            return

        price = intent.observed_price or intent.expected_flea_price
        if not self.guard.reserve_spend(price):
            # Not an error: the session budget is spent. Put the item back so
            # a larger budget on the next run can still reach it.
            self.context.queue.insert(0, intent)
            self.context.current = None
            raise BudgetReached(
                f"Session budget cannot cover {intent.item_name!r} at {price:,}"
            )

        committed = False
        try:
            self.begin_purchase()  # type: ignore[attr-defined]
            if not self.confirm_action(accept=True):
                self.context.skip_current("purchase not confirmed")
                self._recover()
                return

            # Money has now left the stash.
            self.guard.commit_spend(price)
            committed = True

            if not self.enter_trader_menu():
                self.context.skip_current("could not open trader menu")
                self._recover()
                return

            if not self.sell_to_trader(intent):
                self.context.skip_current("sell failed")
                self._recover()
                return

            self.guard.record_sale(intent.expected_trader_price)
            log.info(
                "Completed {!r}: margin {}",
                intent.item_name,
                intent.actual_margin() or intent.expected_margin,
            )
            self.context.complete_current()
        finally:
            # Any exit before the purchase went through must hand the
            # reservation back, or the budget leaks a little on every failure.
            if not committed:
                self.guard.release_spend(price)

    # ------------------------------------------------------------------
    def _back_to_flea(self) -> None:
        self._safe_trigger(Trigger.BACK_TO_FLEA)

    def _recover(self) -> None:
        """Return to a known state after a failure.

        Presses escape a few times — the universal Tarkov "close whatever modal
        I'm stuck behind" — then resets the machine to IDLE.
        """
        log.info("Recovering to IDLE.")
        try:
            for _ in range(3):
                self.input.press("escape")
                self.input.pause(0.3)
        except (BotStopped, SafetyLimitExceeded):
            raise
        except Exception as exc:  # pragma: no cover - best-effort recovery
            log.warning("Recovery input failed: {}", exc)
        self._safe_trigger(Trigger.RESET)

    def _safe_trigger(self, trigger: Trigger) -> bool:
        """Fire a trigger, tolerating it being invalid from the current state."""
        try:
            self.trigger(trigger.value)  # type: ignore[attr-defined]
            return True
        except MachineError as exc:
            log.debug("Trigger {} not valid from {}: {}", trigger.value, self.state, exc)
            return False

    # ------------------------------------------------------------------
    def wait_until_idle(self, timeout: float = 15.0) -> bool:
        """Block until no loading spinner is on screen."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.guard.checkpoint()
            try:
                busy = self.matcher.exists("loading_spinner")
            except FileNotFoundError:
                return True  # no spinner template configured; assume ready
            if not busy:
                return True
            time.sleep(0.3)
        log.warning("Still busy after {:.0f}s", timeout)
        return False
