"""Thread bridge between the GUI and the bot.

Tkinter is not thread-safe: every widget call must happen on the thread that
created the root window. The bot, meanwhile, blocks for seconds at a time on
screen captures and deliberate input delays. So the bot runs on a worker
thread and communicates only through a :class:`queue.Queue` of events, which
the GUI drains on a timer.

Nothing here touches a widget, which is what makes it testable without a
display — see ``tests/test_gui_controller.py``.
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from flea_bot.config import Config
from flea_bot.ledger import LedgerSnapshot
from flea_bot.logging_setup import get_logger

log = get_logger("gui")


class EventType(str, Enum):
    STATUS = "status"
    LEDGER = "ledger"
    LOG = "log"
    TRADE = "trade"
    FINISHED = "finished"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BotEvent:
    type: EventType
    payload: Any = None
    message: str = ""


class BotState(str, Enum):
    IDLE = "idle"
    FETCHING = "fetching"
    RANKING = "ranking"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    FINISHED = "finished"
    ERROR = "error"


@dataclass
class BotController:
    """Owns the worker thread and the event queue.

    The GUI calls :meth:`start`/:meth:`pause`/:meth:`stop` and polls
    :meth:`drain`. It never blocks on the bot.
    """

    config: Config
    events: queue.Queue[BotEvent] = field(default_factory=queue.Queue)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _guard: Any = field(default=None, repr=False)
    _state: BotState = BotState.IDLE
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    @property
    def state(self) -> BotState:
        with self._lock:
            return self._state

    def _set_state(self, state: BotState) -> None:
        with self._lock:
            self._state = state
        self.events.put(BotEvent(EventType.STATUS, payload=state))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def emit(self, type_: EventType, message: str = "", payload: Any = None) -> None:
        self.events.put(BotEvent(type_, payload=payload, message=message))

    def drain(self, limit: int = 200) -> list[BotEvent]:
        """Pull pending events. Called from the GUI thread on a timer.

        Bounded so a burst of log lines can't stall the UI loop.
        """
        out: list[BotEvent] = []
        for _ in range(limit):
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # ------------------------------------------------------------------
    def start(self, *, budget: int | None, dry_run: bool, top_n: int) -> bool:
        """Launch a run on a worker thread. False if one is already going."""
        if self.running:
            log.warning("A run is already in progress.")
            return False

        self.config.general.dry_run = dry_run
        self._thread = threading.Thread(
            target=self._run,
            kwargs={"budget": budget, "top_n": top_n},
            daemon=True,
            name="flea-bot-worker",
        )
        self._thread.start()
        return True

    def pause(self) -> None:
        if self._guard is not None:
            self._guard.toggle_pause()
            self._set_state(
                BotState.PAUSED if self._guard.paused else BotState.RUNNING
            )

    def stop(self) -> None:
        """Trip the kill switch. The worker stops at its next checkpoint."""
        if self._guard is not None:
            self._set_state(BotState.STOPPING)
            self._guard.kill()

    def snapshot(self) -> LedgerSnapshot | None:
        return self._guard.snapshot() if self._guard is not None else None

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------------
    def _run(self, *, budget: int | None, top_n: int) -> None:
        """Worker body. Every exit path must emit FINISHED or ERROR."""
        from flea_bot.database.repository import PriceRepository
        from flea_bot.decision.ranking import rank_items
        from flea_bot.orchestrator.machine import FleaBotMachine
        from flea_bot.safety import RunGuard
        from flea_bot.scraper.base import fetch_with_fallback
        from flea_bot.traders.prices import TraderPriceBook

        repo = None
        try:
            self._set_state(BotState.FETCHING)
            self.emit(EventType.LOG, "Fetching prices...")
            items, source = fetch_with_fallback(self.config)
            self.emit(EventType.LOG, f"Loaded {len(items)} items from {source}.")

            self._set_state(BotState.RANKING)
            repo = PriceRepository(self.config)
            repo.insert_snapshots(items, source=source)
            book = TraderPriceBook.load(config=self.config)
            ranked = rank_items(
                book.compute_margins(items),
                stats=repo.stats_for_all(source=source),
                config=self.config,
                top_n=top_n,
            )

            if not ranked.items:
                self.emit(
                    EventType.LOG,
                    f"Nothing passed the filters. Rejections: {ranked.rejection_summary()}",
                )
                self._set_state(BotState.FINISHED)
                self.emit(EventType.FINISHED, "No profitable trades found.")
                return

            self.emit(EventType.LOG, f"{len(ranked.items)} candidate trade(s).")

            with RunGuard(self.config, budget=budget) as guard:
                self._guard = guard
                machine = FleaBotMachine(self.config, guard=guard)
                machine.queue_trades(ranked.items)

                self._set_state(BotState.RUNNING)
                self.emit(EventType.LEDGER, payload=guard.snapshot())

                # Push a ledger update after every trade so the counters move
                # during the run rather than all at once at the end.
                original = machine._execute_trade

                def traced(intent):
                    try:
                        return original(intent)
                    finally:
                        self.emit(EventType.LEDGER, payload=guard.snapshot())
                        self.emit(EventType.TRADE, message=intent.item_name)

                machine._execute_trade = traced  # type: ignore[method-assign]

                context = machine.run()
                self.emit(EventType.LEDGER, payload=guard.snapshot())

            self._set_state(BotState.FINISHED)
            self.emit(
                EventType.FINISHED,
                f"Done: {len(context.completed)} completed, "
                f"{len(context.skipped)} skipped.",
                payload=context.summary(),
            )

        except Exception as exc:
            log.exception("Bot worker failed")
            self._set_state(BotState.ERROR)
            self.emit(
                EventType.ERROR,
                f"{type(exc).__name__}: {exc}",
                payload=traceback.format_exc(),
            )
        finally:
            if repo is not None:
                repo.dispose()
            self._guard = None
