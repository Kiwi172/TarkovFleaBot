"""Runtime safety: pause/kill hotkeys, runaway guards, spend cap, dry-run flag.

:class:`RunGuard` is the single object every acting component consults before
it does anything. It owns:

* the pause / stop events driven by global hotkeys,
* a hard cap on actions per run,
* a **hard cap on roubles spent per session**,
* a consecutive-vision-failure circuit breaker,
* the ``dry_run`` flag.

``checkpoint()`` is the choke point — call it before each action. It blocks
while paused and raises :class:`BotStopped` once the kill switch is hit.

The spend cap lives here rather than in the GUI on purpose: it must hold for
headless runs, scheduled runs, and runs where the GUI has crashed. A budget
that only exists in a window is not a budget.
"""

from __future__ import annotations

import threading
from types import TracebackType

from flea_bot.config import Config
from flea_bot.ledger import BudgetExceeded, LedgerSnapshot, SessionLedger
from flea_bot.logging_setup import get_logger

log = get_logger("safety")


class BotStopped(RuntimeError):
    """Raised at a checkpoint after the kill switch fires."""


class SafetyLimitExceeded(RuntimeError):
    """Raised when an action cap or failure-streak limit trips."""


class RunGuard:
    """Owns the stop/pause state for one bot run.

    Usable as a context manager so hotkeys are always unregistered::

        with RunGuard(config) as guard:
            guard.checkpoint()
    """

    def __init__(self, config: Config, *, budget: int | None = None) -> None:
        self.config = config
        self.dry_run = config.general.dry_run
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()  # not paused initially
        self._action_count = 0
        self._consecutive_failures = 0
        self._hotkeys_registered = False
        self._hooks: list[object] = []

        # A budget of 0 or less in config means "unlimited"; an explicit
        # `budget` argument (from the GUI) overrides config either way.
        configured = config.safety.max_spend_per_session
        effective = budget if budget is not None else (configured if configured > 0 else None)
        self.ledger = SessionLedger(budget=effective)
        self.ledger.start()
        if effective is not None:
            log.info("Session spend cap: {:,} roubles", effective)
        else:
            log.warning("No session spend cap set — the bot may spend without limit.")

    # ------------------------------------------------------------------
    # hotkeys
    # ------------------------------------------------------------------
    def register_hotkeys(self) -> bool:
        """Bind the global pause/kill hotkeys.

        Returns False (with a warning) when the ``keyboard`` backend is
        unavailable — on Linux it needs root for /dev/input access. The bot
        still runs; you just lose the panic button, so we say so loudly.
        """
        if self._hotkeys_registered:
            return True
        try:
            import keyboard  # noqa: PLC0415 - optional, platform-dependent
        except Exception as exc:  # pragma: no cover - import-environment dependent
            log.warning("Hotkeys unavailable ({}): no pause/kill key this run.", exc)
            return False

        try:
            keyboard.add_hotkey(self.config.safety.pause_hotkey, self.toggle_pause)
            keyboard.add_hotkey(self.config.safety.kill_hotkey, self.kill)
        except Exception as exc:  # pragma: no cover - needs a real input device
            log.warning(
                "Could not register hotkeys ({}). On Linux this usually means "
                "the process needs root to read /dev/input.",
                exc,
            )
            return False

        self._hotkeys_registered = True
        log.info(
            "Hotkeys armed: {} = pause/resume, {} = kill.",
            self.config.safety.pause_hotkey.upper(),
            self.config.safety.kill_hotkey.upper(),
        )
        return True

    def unregister_hotkeys(self) -> None:
        if not self._hotkeys_registered:
            return
        try:
            import keyboard  # noqa: PLC0415

            keyboard.remove_all_hotkeys()
        except Exception:  # pragma: no cover - best effort teardown
            pass
        self._hotkeys_registered = False

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def action_count(self) -> int:
        return self._action_count

    def kill(self) -> None:
        """Trip the kill switch. Safe to call from a hotkey thread."""
        if not self._stop.is_set():
            log.warning("KILL SWITCH — stopping at next checkpoint.")
        self._stop.set()
        self._resume.set()  # unblock anyone waiting in pause

    def pause(self) -> None:
        if self._resume.is_set():
            log.warning("PAUSED — press {} to resume.", self.config.safety.pause_hotkey.upper())
        self._resume.clear()

    def resume(self) -> None:
        if not self._resume.is_set():
            log.info("Resumed.")
        self._resume.set()

    def toggle_pause(self) -> None:
        self.resume() if self.paused else self.pause()

    # ------------------------------------------------------------------
    # the choke point
    # ------------------------------------------------------------------
    def checkpoint(self, *, counts_as_action: bool = False) -> None:
        """Block while paused; raise if stopped or over a limit.

        Call before every input dispatch and between orchestrator steps.
        """
        if self._stop.is_set():
            raise BotStopped("Kill switch engaged")

        if self.paused:
            # Wake up periodically so a kill during a pause is still honoured.
            while not self._resume.wait(timeout=0.2):
                if self._stop.is_set():
                    raise BotStopped("Kill switch engaged while paused")

        if self._stop.is_set():
            raise BotStopped("Kill switch engaged")

        if counts_as_action:
            self._action_count += 1
            limit = self.config.safety.max_actions_per_run
            if self._action_count > limit:
                self.kill()
                raise SafetyLimitExceeded(
                    f"Action cap reached ({limit}). Raise safety.max_actions_per_run "
                    f"if this run legitimately needs more."
                )

    # ------------------------------------------------------------------
    # spend cap
    # ------------------------------------------------------------------
    def can_afford(self, price: int) -> bool:
        """Would buying at ``price`` stay inside the session budget?"""
        return self.ledger.can_afford(price)

    def reserve_spend(self, price: int) -> bool:
        """Claim ``price`` against the budget before buying.

        Returns False (and logs) when it doesn't fit, rather than raising —
        exhausting the budget is a normal, expected end to a session, not an
        error. The orchestrator treats it as "stop queueing trades".
        """
        try:
            self.ledger.reserve(price)
        except BudgetExceeded as exc:
            log.warning("{}", exc)
            return False
        return True

    def commit_spend(self, reserved: int, actual: int | None = None) -> None:
        """Record a completed purchase against a prior reservation."""
        self.ledger.commit(reserved, actual)
        snap = self.ledger.snapshot()
        log.info(
            "Bought for {:,} — spent {:,}{}, net {:+,}",
            actual if actual is not None else reserved,
            snap.spent,
            f"/{snap.budget:,}" if snap.budget else "",
            snap.net,
        )

    def release_spend(self, reserved: int) -> None:
        """Return a reservation for a purchase that didn't complete."""
        self.ledger.release(reserved)

    def record_sale(self, amount: int) -> None:
        self.ledger.record_sale(amount)
        snap = self.ledger.snapshot()
        log.info("Sold for {:,} — earned {:,}, net {:+,}", amount, snap.earned, snap.net)

    def snapshot(self) -> LedgerSnapshot:
        """Current ledger state. Safe to call from the GUI thread."""
        return self.ledger.snapshot()

    @property
    def budget_exhausted(self) -> bool:
        """True when there isn't enough left to be worth continuing."""
        remaining = self.ledger.snapshot().remaining
        return remaining is not None and remaining <= 0

    # ------------------------------------------------------------------
    # failure circuit breaker
    # ------------------------------------------------------------------
    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self, reason: str = "") -> None:
        self._consecutive_failures += 1
        limit = self.config.safety.max_consecutive_failures
        log.warning(
            "Vision/step failure {}/{}{}",
            self._consecutive_failures,
            limit,
            f": {reason}" if reason else "",
        )
        if self._consecutive_failures >= limit:
            self.kill()
            raise SafetyLimitExceeded(
                f"{limit} consecutive failures — the bot has lost track of the UI. "
                f"Stopping rather than clicking blind."
            )

    # ------------------------------------------------------------------
    def __enter__(self) -> RunGuard:
        self.register_hotkeys()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.unregister_hotkeys()
        snap = self.ledger.snapshot()
        log.info(
            "Run finished after {} action(s). Spent {:,}, earned {:,}, net {:+,} "
            "({} buy(s), {} sale(s)).",
            self._action_count,
            snap.spent,
            snap.earned,
            snap.net,
            snap.purchases,
            snap.sales,
        )
