"""Mouse and keyboard actions with humanised timing.

Two reasons the delays and jitter exist, and neither is anti-cheat evasion
(SPT is offline and has none):

1. **The UI drops perfectly-timed input.** Unity UIs debounce; a click
   dispatched 0ms after a mouse-move frequently lands before the hover state
   registers and does nothing. Real pauses make the automation *work*.
2. **Determinism hides bugs.** Always clicking the exact centre pixel of a
   button means a half-pixel coordinate error never surfaces until the day the
   layout shifts. A few pixels of jitter surfaces it immediately.

Every method routes through :meth:`_act`, which consults the
:class:`~flea_bot.safety.RunGuard` and honours dry-run. There is no code path
that dispatches input without passing that check.
"""

from __future__ import annotations

import random
import time
from collections.abc import Sequence

from flea_bot.config import Config, get_config
from flea_bot.input.backends import InputBackend, get_backend
from flea_bot.logging_setup import get_logger, log_action
from flea_bot.safety import RunGuard

log = get_logger("input")

Point = tuple[int, int]


class InputController:
    """The only thing in the project allowed to move the mouse."""

    def __init__(
        self,
        config: Config | None = None,
        guard: RunGuard | None = None,
        backend: InputBackend | None = None,
    ) -> None:
        self.config = config or get_config()
        self.guard = guard or RunGuard(self.config)
        self.dry_run = self.guard.dry_run
        self.backend = backend or get_backend(dry_run=self.dry_run)
        self._cfg = self.config.input

    # ------------------------------------------------------------------
    # timing helpers
    # ------------------------------------------------------------------
    def _delay(self) -> float:
        return random.uniform(self._cfg.min_action_delay, self._cfg.max_action_delay)

    def _move_duration(self) -> float:
        return random.uniform(self._cfg.min_move_duration, self._cfg.max_move_duration)

    def pause(self, seconds: float | None = None) -> None:
        """Sleep for a randomised beat. Skipped entirely in dry-run."""
        if self.dry_run:
            return
        time.sleep(seconds if seconds is not None else self._delay())

    def _jitter(self, point: Point) -> Point:
        j = self._cfg.click_jitter_px
        if j <= 0:
            return point
        return (point[0] + random.randint(-j, j), point[1] + random.randint(-j, j))

    def _act(self, action: str, **fields: object) -> bool:
        """Safety gate for every dispatch.

        Returns True if the caller should actually touch the backend. Always
        logs, so dry-run and live runs produce comparable logs.
        """
        self.guard.checkpoint(counts_as_action=True)
        log_action("input", action, dry_run=self.dry_run, **fields)
        return not self.dry_run

    # ------------------------------------------------------------------
    # mouse
    # ------------------------------------------------------------------
    def move(self, point: Point, *, duration: float | None = None) -> None:
        """Glide the cursor to a point over several steps.

        Teleporting the cursor makes Unity miss the hover state that many
        Tarkov controls need before they accept a click.
        """
        target = self._jitter(point)
        if not self._act("move", to=target):
            return

        start = self.backend.position()
        span = duration if duration is not None else self._move_duration()
        steps = max(2, int(span / 0.012))

        for i in range(1, steps + 1):
            t = i / steps
            # Ease-in-out: accelerate away, decelerate onto the target.
            eased = t * t * (3.0 - 2.0 * t)
            x = round(start[0] + (target[0] - start[0]) * eased)
            y = round(start[1] + (target[1] - start[1]) * eased)
            self.backend.move_to(x, y)
            time.sleep(span / steps)

        self.backend.move_to(*target)

    def click(
        self,
        point: Point | None = None,
        *,
        button: str = "left",
        clicks: int = 1,
        move_first: bool = True,
    ) -> None:
        """Click, optionally moving to ``point`` first."""
        if point is not None and move_first:
            self.move(point)
            self.pause(self._cfg.post_click_delay * random.uniform(0.5, 1.0))

        target = point or self.backend.position()
        for i in range(clicks):
            if not self._act("click", at=target, button=button, n=i + 1):
                continue
            self.backend.click(button=button)
            if i + 1 < clicks:
                # Well under the OS double-click threshold.
                time.sleep(random.uniform(0.05, 0.11))

        self.pause(self._cfg.post_click_delay)

    def double_click(self, point: Point | None = None, **kwargs) -> None:
        self.click(point, clicks=2, **kwargs)

    def right_click(self, point: Point | None = None, **kwargs) -> None:
        self.click(point, button="right", **kwargs)

    def drag(
        self,
        start: Point,
        end: Point,
        *,
        button: str = "left",
        duration: float | None = None,
    ) -> None:
        """Press at ``start``, glide to ``end``, release.

        The pauses after mouse-down and before mouse-up are not cosmetic:
        Tarkov's inventory needs the drag to be held briefly before it treats
        the gesture as a drag rather than a click.
        """
        self.move(start)
        self.pause()

        if self._act("mouse_down", at=start, button=button):
            self.backend.mouse_down(button=button)
        self.pause(random.uniform(0.06, 0.14))

        self.move(end, duration=duration or self._move_duration() * 1.6)
        self.pause(random.uniform(0.06, 0.14))

        if self._act("mouse_up", at=end, button=button):
            self.backend.mouse_up(button=button)
        self.pause(self._cfg.post_click_delay)

    def drag_and_drop(self, start: Point, end: Point, **kwargs) -> None:
        """Alias for :meth:`drag`, named for how the FSM talks about it."""
        self.drag(start, end, **kwargs)

    def scroll(self, clicks: int, point: Point | None = None) -> None:
        if point is not None:
            self.move(point)
        if self._act("scroll", clicks=clicks, at=point):
            self.backend.scroll(clicks)
        self.pause()

    # ------------------------------------------------------------------
    # keyboard
    # ------------------------------------------------------------------
    def press(self, key: str, *, presses: int = 1) -> None:
        for i in range(presses):
            if self._act("press", key=key, n=i + 1):
                self.backend.key_down(key)
                time.sleep(random.uniform(0.03, 0.09))  # realistic hold time
                self.backend.key_up(key)
            self.pause()

    def hotkey(self, *keys: str) -> None:
        """Press a chord (``ctrl+a``), releasing in reverse order."""
        if not self._act("hotkey", keys=keys):
            return
        for key in keys:
            self.backend.key_down(key)
            time.sleep(random.uniform(0.02, 0.05))
        for key in reversed(keys):
            self.backend.key_up(key)
        self.pause()

    def type_text(self, text: str, *, per_char: bool = True) -> None:
        """Type a string with per-keystroke delays.

        Per-character is the default because Tarkov's search box filters as you
        type; a bulk ``write`` frequently drops characters.
        """
        if not self._act("type", text=text):
            return
        if not per_char:
            self.backend.write(text)
        else:
            for char in text:
                self.backend.write(char)
                time.sleep(random.uniform(0.04, 0.13))
        self.pause()

    def clear_field(self, point: Point | None = None) -> None:
        """Select-all + delete, the reliable way to empty a text input."""
        if point is not None:
            self.click(point)
        self.hotkey("ctrl", "a")
        self.press("delete")

    def type_into(self, point: Point, text: str) -> None:
        """Click a field, clear it, type into it. The flea-search primitive."""
        self.clear_field(point)
        self.type_text(text)

    # ------------------------------------------------------------------
    def click_sequence(self, points: Sequence[Point], *, gap: float | None = None) -> None:
        """Click several points in order, pausing between each."""
        for point in points:
            self.click(point)
            self.pause(gap)
