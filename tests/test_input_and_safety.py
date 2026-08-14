"""Input controller and safety guard.

Everything runs against NullBackend, so these tests assert on *recorded intent*
rather than real cursor movement.
"""

from __future__ import annotations

import threading
import time

import pytest

from flea_bot.input.backends import NullBackend, get_backend
from flea_bot.input.controller import InputController
from flea_bot.safety import BotStopped, RunGuard, SafetyLimitExceeded


@pytest.fixture
def guard(config) -> RunGuard:
    return RunGuard(config)


@pytest.fixture
def live_controller(config):
    """Controller with dry_run off but a Null backend, so calls are recorded."""
    config.general.dry_run = False
    config.input.min_action_delay = 0
    config.input.max_action_delay = 0
    config.input.post_click_delay = 0
    config.input.min_move_duration = 0
    config.input.max_move_duration = 0
    guard = RunGuard(config)
    backend = NullBackend()
    return InputController(config, guard=guard, backend=backend), backend


class TestBackendSelection:
    def test_dry_run_always_gets_null_backend(self):
        assert get_backend(dry_run=True).name == "null"

    def test_dry_run_wins_over_prefer(self):
        # The dry-run guarantee must not be overridable by a preference.
        assert get_backend(dry_run=True, prefer="pydirectinput").name == "null"


class TestDryRun:
    def test_no_backend_calls_in_dry_run(self, config):
        config.general.dry_run = True
        backend = NullBackend()
        ctrl = InputController(config, guard=RunGuard(config), backend=backend)

        ctrl.click((100, 200))
        ctrl.drag((0, 0), (50, 50))
        ctrl.type_text("Bottle of water")
        ctrl.press("enter")

        assert backend.calls == [], "dry-run must never dispatch input"

    def test_dry_run_still_counts_actions(self, config):
        config.general.dry_run = True
        g = RunGuard(config)
        ctrl = InputController(config, guard=g, backend=NullBackend())
        ctrl.click((10, 10))
        assert g.action_count > 0, "dry-run must still be subject to the action cap"


class TestClicking:
    def test_click_moves_then_clicks(self, live_controller):
        ctrl, backend = live_controller
        ctrl.click((300, 400))
        actions = [c[0] for c in backend.calls]
        assert "move_to" in actions
        assert actions[-1] == "click"

    def test_click_lands_within_jitter_of_target(self, live_controller):
        ctrl, backend = live_controller
        ctrl.config.input.click_jitter_px = 3
        ctrl.click((300, 400))
        final_move = [c for c in backend.calls if c[0] == "move_to"][-1]
        x, y = final_move[1]
        assert abs(x - 300) <= 3 and abs(y - 400) <= 3

    def test_zero_jitter_hits_exactly(self, live_controller):
        ctrl, backend = live_controller
        ctrl.config.input.click_jitter_px = 0
        ctrl.click((300, 400))
        assert [c for c in backend.calls if c[0] == "move_to"][-1][1] == (300, 400)

    def test_double_click_issues_two_clicks(self, live_controller):
        ctrl, backend = live_controller
        ctrl.double_click((10, 10))
        assert sum(1 for c in backend.calls if c[0] == "click") == 2

    def test_right_click_uses_right_button(self, live_controller):
        ctrl, backend = live_controller
        ctrl.right_click((10, 10))
        assert [c for c in backend.calls if c[0] == "click"][0][1] == ("right",)


class TestDragAndType:
    def test_drag_order_is_down_move_up(self, live_controller):
        ctrl, backend = live_controller
        ctrl.drag((10, 10), (200, 200))
        actions = [c[0] for c in backend.calls]
        down = actions.index("mouse_down")
        up = actions.index("mouse_up")
        assert down < up
        assert "move_to" in actions[down:up], "must move while the button is held"

    def test_type_text_writes_each_character(self, live_controller):
        ctrl, backend = live_controller
        ctrl.type_text("abc")
        writes = [c[1][0] for c in backend.calls if c[0] == "write"]
        assert writes == ["a", "b", "c"]

    def test_bulk_write_when_per_char_disabled(self, live_controller):
        ctrl, backend = live_controller
        ctrl.type_text("abc", per_char=False)
        assert [c[1][0] for c in backend.calls if c[0] == "write"] == ["abc"]

    def test_press_holds_then_releases(self, live_controller):
        ctrl, backend = live_controller
        ctrl.press("enter")
        assert [c[0] for c in backend.calls] == ["key_down", "key_up"]

    def test_hotkey_releases_in_reverse(self, live_controller):
        ctrl, backend = live_controller
        ctrl.hotkey("ctrl", "a")
        keys = [(c[0], c[1][0]) for c in backend.calls]
        assert keys == [
            ("key_down", "ctrl"),
            ("key_down", "a"),
            ("key_up", "a"),
            ("key_up", "ctrl"),
        ]


class TestRunGuard:
    def test_checkpoint_passes_when_running(self, guard):
        guard.checkpoint()  # must not raise

    def test_checkpoint_raises_after_kill(self, guard):
        guard.kill()
        with pytest.raises(BotStopped):
            guard.checkpoint()

    def test_action_cap_trips(self, config):
        config.safety.max_actions_per_run = 3
        g = RunGuard(config)
        for _ in range(3):
            g.checkpoint(counts_as_action=True)
        with pytest.raises(SafetyLimitExceeded, match="Action cap"):
            g.checkpoint(counts_as_action=True)

    def test_action_cap_also_stops_the_guard(self, config):
        config.safety.max_actions_per_run = 1
        g = RunGuard(config)
        g.checkpoint(counts_as_action=True)
        with pytest.raises(SafetyLimitExceeded):
            g.checkpoint(counts_as_action=True)
        assert g.stopped

    def test_failure_streak_trips_breaker(self, config):
        config.safety.max_consecutive_failures = 3
        g = RunGuard(config)
        g.record_failure("a")
        g.record_failure("b")
        with pytest.raises(SafetyLimitExceeded, match="consecutive failures"):
            g.record_failure("c")

    def test_success_resets_the_streak(self, config):
        config.safety.max_consecutive_failures = 2
        g = RunGuard(config)
        g.record_failure("a")
        g.record_success()
        g.record_failure("b")  # streak restarted, so this must not raise

    def test_pause_blocks_until_resumed(self, guard):
        guard.pause()
        released = threading.Event()

        def worker():
            guard.checkpoint()
            released.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert not released.wait(timeout=0.3), "checkpoint must block while paused"

        guard.resume()
        assert released.wait(timeout=2.0), "checkpoint must release on resume"

    def test_kill_during_pause_unblocks_and_raises(self, guard):
        guard.pause()
        result = {}

        def worker():
            try:
                guard.checkpoint()
            except BotStopped:
                result["stopped"] = True

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.1)
        guard.kill()
        t.join(timeout=2.0)
        assert result.get("stopped"), "kill must break out of a pause"

    def test_toggle_pause(self, guard):
        assert not guard.paused
        guard.toggle_pause()
        assert guard.paused
        guard.toggle_pause()
        assert not guard.paused

    def test_controller_stops_mid_sequence(self, config):
        config.general.dry_run = False
        config.input.min_action_delay = config.input.max_action_delay = 0
        config.input.post_click_delay = 0
        config.input.min_move_duration = config.input.max_move_duration = 0
        g = RunGuard(config)
        backend = NullBackend()
        ctrl = InputController(config, guard=g, backend=backend)

        ctrl.click((10, 10))
        before = len(backend.calls)
        g.kill()
        with pytest.raises(BotStopped):
            ctrl.click((20, 20))
        assert len(backend.calls) == before, "no input after the kill switch"
