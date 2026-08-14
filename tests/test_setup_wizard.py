"""Setup wizard logic, driven with scripted prompts instead of a real cursor."""

from __future__ import annotations

import pytest

from flea_bot.setup_wizard import (
    STEPS,
    CalibrationWizard,
    WizardAborted,
    strip_window_sections,
    write_config,
)


class FakeBackend:
    """Yields a scripted sequence of cursor positions."""

    def __init__(self, positions):
        self.positions = list(positions)
        self.calls = 0

    def position(self):
        pos = self.positions[min(self.calls, len(self.positions) - 1)]
        self.calls += 1
        return pos


def make_wizard(config, positions, monkeypatch, answers=()):
    answers = list(answers)
    wizard = CalibrationWizard(config, prompt=lambda m: answers.pop(0) if answers else "",
                               notify=lambda m: None)
    # One shared backend: the wizard reads the cursor twice per region and must
    # see the sequence advance, not restart.
    backend = FakeBackend(positions)
    monkeypatch.setattr(wizard, "_cursor", lambda: backend)
    return wizard


class TestRegionCapture:
    def test_two_corners_become_a_region(self, config, monkeypatch):
        w = make_wizard(config, [(100, 200), (300, 260)], monkeypatch)
        assert w.capture_region(STEPS[1]) == (100, 200, 200, 60)

    def test_corners_normalised_when_dragged_backwards(self, config, monkeypatch):
        """Bottom-right first must not yield a negative-size region."""
        w = make_wizard(config, [(300, 260), (100, 200)], monkeypatch)
        assert w.capture_region(STEPS[1]) == (100, 200, 200, 60)

    def test_tiny_region_rejected(self, config, monkeypatch):
        w = make_wizard(config, [(100, 100), (102, 101)], monkeypatch)
        with pytest.raises(ValueError, match="too small"):
            w.capture_region(STEPS[1])

    def test_retry_declined_aborts(self, config, monkeypatch):
        w = make_wizard(config, [(10, 10), (11, 11)], monkeypatch, answers=["", "", "n"])
        with pytest.raises(WizardAborted):
            w.run(only=["search_box"])


class TestConfigRendering:
    def test_renders_window_and_regions(self, config, monkeypatch):
        w = make_wizard(config, [(0, 0), (10, 10)], monkeypatch)
        w.regions = {"search_box": (640, 180, 400, 32)}
        w.templates = {}
        out = w.render_config((0, 0, 1920, 1080))

        assert "[window]" in out
        assert "width = 1920" in out
        assert "search_box = [640, 180, 400, 32]" in out

    def test_template_paths_use_forward_slashes(self, config, monkeypatch):
        """TOML on Windows must not emit unescaped backslashes."""
        from pathlib import PurePosixPath

        w = make_wizard(config, [(0, 0), (10, 10)], monkeypatch)
        w.regions = {}
        w.templates = {"flea_market_tab": PurePosixPath("assets/templates/flea_market_tab.png")}
        out = w.render_config((0, 0, 100, 100))
        assert 'flea_market_tab = "assets/templates/flea_market_tab.png"' in out
        assert "\\" not in out

    def test_rendered_config_is_valid_toml(self, config, monkeypatch):
        import tomllib

        w = make_wizard(config, [(0, 0), (10, 10)], monkeypatch)
        w.regions = {"search_box": (1, 2, 3, 4), "offer_list": (5, 6, 7, 8)}
        w.templates = {}
        parsed = tomllib.loads(w.render_config((0, 0, 1920, 1080)))

        assert parsed["window"]["width"] == 1920
        assert parsed["window"]["regions"]["search_box"] == [1, 2, 3, 4]


class TestSectionPreservation:
    SAMPLE = """\
[general]
dry_run = true

[thresholds]
min_margin = 12345

[window]
left = 0
width = 800

[window.regions]
search_box = [1, 2, 3, 4]

[safety]
kill_hotkey = "f10"
"""

    def test_strips_only_window_sections(self):
        out = strip_window_sections(self.SAMPLE)
        assert "[window]" not in out
        assert "[window.regions]" not in out
        assert "search_box" not in out
        # Everything else survives.
        assert "min_margin = 12345" in out
        assert 'kill_hotkey = "f10"' in out
        assert "dry_run = true" in out

    def test_rewrite_preserves_user_thresholds(self, config, tmp_path, monkeypatch):
        """Re-running calibration must not reset tuned settings."""
        import tomllib

        target = tmp_path / "config.toml"
        target.write_text(self.SAMPLE)

        w = make_wizard(config, [(0, 0), (10, 10)], monkeypatch)
        w.regions = {"search_box": (9, 9, 9, 9)}
        w.templates = {}
        write_config(w, (0, 0, 1920, 1080), target)

        parsed = tomllib.loads(target.read_text())
        assert parsed["thresholds"]["min_margin"] == 12345, "user setting must survive"
        assert parsed["window"]["width"] == 1920, "new calibration must apply"
        assert parsed["window"]["regions"]["search_box"] == [9, 9, 9, 9]

    def test_existing_config_is_backed_up(self, config, tmp_path, monkeypatch):
        target = tmp_path / "config.toml"
        target.write_text(self.SAMPLE)

        w = make_wizard(config, [(0, 0), (10, 10)], monkeypatch)
        w.regions, w.templates = {}, {}
        write_config(w, (0, 0, 100, 100), target)

        backup = target.with_suffix(".toml.bak")
        assert backup.is_file()
        assert "min_margin = 12345" in backup.read_text()


class TestSteps:
    def test_every_step_has_a_hint(self):
        assert all(s.hint and s.label for s in STEPS)

    def test_step_keys_unique(self):
        keys = [s.key for s in STEPS]
        assert len(keys) == len(set(keys))

    def test_template_steps_cover_orchestrator_needs(self):
        """Every template the state machine clicks must be calibratable."""
        from flea_bot.orchestrator.machine import STATE_TEMPLATES

        calibratable = {s.key for s in STEPS if s.is_template}
        assert set(STATE_TEMPLATES.values()) <= calibratable
        for needed in ("sell_button", "confirm_button", "offer_row"):
            assert needed in calibratable
