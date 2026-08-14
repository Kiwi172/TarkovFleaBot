"""Interactive calibration: turn a fresh clone into a configured install.

This exists because the genuinely fiddly part of setup is not installing
Python — it's that template images and screen regions are specific to your
resolution, UI scale and mod list, so they cannot ship with the repo. The
wizard walks you through capturing them and writes a valid ``config.toml``.

Flow per item: you hover the two corners of a region and tap a key at each,
the wizard reads the cursor position, grabs that box, and saves it. No typing
coordinates by hand.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from flea_bot.config import PROJECT_ROOT, Config, Region
from flea_bot.logging_setup import get_logger

log = get_logger("setup")


@dataclass(frozen=True, slots=True)
class CalibrationStep:
    """One thing the user must point at."""

    key: str
    label: str
    hint: str
    # "template" -> also save a PNG; "region" -> coordinates only.
    kind: str = "region"

    @property
    def is_template(self) -> bool:
        return self.kind == "template"


# Ordered so the user moves through the UI naturally rather than jumping
# between screens.
STEPS: tuple[CalibrationStep, ...] = (
    CalibrationStep(
        "flea_market_tab",
        "Flea Market tab",
        "The tab/button that opens the flea market.",
        "template",
    ),
    CalibrationStep(
        "search_box",
        "Search box",
        "The flea market's item search field.",
    ),
    CalibrationStep(
        "offer_list",
        "Offer list area",
        "The whole region where search results appear.",
    ),
    CalibrationStep(
        "offer_row",
        "A single offer row",
        "One result row — search for a common item first so a row is visible.",
        "template",
    ),
    CalibrationStep(
        "first_offer_price",
        "Price of the first offer",
        "Just the price number on the top result row.",
    ),
    CalibrationStep(
        "first_offer_quantity",
        "Quantity of the first offer",
        "The stack count on the top result row.",
    ),
    CalibrationStep(
        "player_balance",
        "Your rouble balance",
        "The money counter, usually top-right.",
    ),
    CalibrationStep(
        "confirm_button",
        "Confirm button",
        "The confirm/accept button on a purchase dialog.",
        "template",
    ),
    CalibrationStep(
        "cancel_button",
        "Cancel button",
        "The cancel/close button on that same dialog.",
        "template",
    ),
    CalibrationStep(
        "trader_tab",
        "Trader tab",
        "The tab/button that opens a trader.",
        "template",
    ),
    CalibrationStep(
        "sell_button",
        "Sell button",
        "The button that sells selected items to a trader.",
        "template",
    ),
)


class WizardAborted(RuntimeError):
    """The user backed out."""


class CalibrationWizard:
    """Drives calibration. UI-agnostic so both the CLI and GUI can host it.

    ``prompt`` shows a message and blocks until the user is ready; ``notify``
    reports progress. The CLI passes ``input``/``print``; the GUI passes its
    own dialog handlers.
    """

    def __init__(
        self,
        config: Config,
        *,
        prompt: Callable[[str], str],
        notify: Callable[[str], None],
    ) -> None:
        self.config = config
        self.prompt = prompt
        self.notify = notify
        self.regions: dict[str, Region] = dict(config.window.regions)
        self.templates: dict[str, Path] = dict(config.window.templates)

    # ------------------------------------------------------------------
    def _cursor(self):
        from flea_bot.input.backends import get_backend

        return get_backend()

    def read_corner(self, which: str) -> tuple[int, int]:
        """Ask the user to hover a corner, then read the cursor position."""
        self.prompt(f"    Move the mouse to the {which} corner, then press Enter...")
        x, y = self._cursor().position()
        self.notify(f"      got ({x}, {y})")
        return x, y

    def capture_region(self, step: CalibrationStep) -> Region:
        """Read two corners and normalise them into a positive-area box.

        Corners are normalised rather than assumed top-left-first, because
        people drag in whichever direction is comfortable and a negative width
        would silently produce an unusable region.
        """
        x1, y1 = self.read_corner("TOP-LEFT")
        x2, y2 = self.read_corner("BOTTOM-RIGHT")

        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)

        if width < 4 or height < 4:
            raise ValueError(
                f"That region is only {width}x{height}px — too small to match "
                f"reliably. Try again with the corners further apart."
            )
        return (left, top, width, height)

    def save_template(self, step: CalibrationStep, region: Region) -> Path:
        from flea_bot.vision.capture import ScreenCapture

        capture = ScreenCapture(self.config)
        try:
            frame = capture.grab(region)
            target = PROJECT_ROOT / "assets" / "templates" / f"{step.key}.png"
            capture.save(frame, target)
        finally:
            capture.close()
        return target

    # ------------------------------------------------------------------
    def run_step(self, step: CalibrationStep, index: int, total: int) -> None:
        self.notify(f"\n[{index}/{total}] {step.label}")
        self.notify(f"    {step.hint}")

        region = self.capture_region(step)
        self.regions[step.key] = region
        self.notify(f"    region = {list(region)}")

        if step.is_template:
            path = self.save_template(step, region)
            self.templates[step.key] = path.relative_to(PROJECT_ROOT)
            self.notify(f"    saved  {path.name}")

    def run(self, *, only: list[str] | None = None) -> None:
        """Walk every step (or just ``only``)."""
        steps = [s for s in STEPS if only is None or s.key in only]
        total = len(steps)
        for i, step in enumerate(steps, 1):
            while True:
                try:
                    self.run_step(step, i, total)
                    break
                except ValueError as exc:
                    self.notify(f"    ! {exc}")
                    if self.prompt("    Retry this step? [Y/n] ").strip().lower() == "n":
                        raise WizardAborted(f"Aborted at {step.key}") from exc

    # ------------------------------------------------------------------
    def detect_window(self) -> Region:
        """Ask for the game window's own bounds."""
        self.notify("\nFirst, the game window itself.")
        self.notify("  Put SPT in windowed or borderless mode and make it visible.")
        return self.capture_region(
            CalibrationStep("window", "Game window", "The whole game window.")
        )

    # ------------------------------------------------------------------
    def render_config(self, window: Region, *, existing: str | None = None) -> str:
        """Produce the ``[window]`` sections as TOML text.

        Only the calibrated sections are emitted; everything else in the user's
        config is preserved by :func:`write_config`.
        """
        left, top, width, height = window
        lines = [
            "[window]",
            f"left = {left}",
            f"top = {top}",
            f"width = {width}",
            f"height = {height}",
            f"monitor = {self.config.window.monitor}",
            "",
            "[window.regions]",
        ]
        for key in sorted(self.regions):
            lines.append(f"{key} = {list(self.regions[key])}")

        lines += ["", "[window.templates]"]
        for key in sorted(self.templates):
            lines.append(f'{key} = "{self.templates[key].as_posix()}"')

        return "\n".join(lines) + "\n"


def strip_window_sections(text: str) -> str:
    """Remove existing ``[window*]`` sections so they can be replaced.

    Keeps every other section byte-for-byte — re-running the wizard must not
    clobber thresholds or price-source settings the user has tuned.
    """
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped.startswith("[window")
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def write_config(wizard: CalibrationWizard, window: Region, path: Path) -> Path:
    """Write calibration into ``path``, preserving unrelated settings.

    Backs up any existing file first — losing a tuned config to a mistyped
    calibration run would be a miserable way to lose an evening.
    """
    if path.is_file():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        base = strip_window_sections(path.read_text(encoding="utf-8"))
        log.info("Backed up existing config to {}", backup.name)
    else:
        example = PROJECT_ROOT / "config" / "config.example.toml"
        base = strip_window_sections(example.read_text(encoding="utf-8"))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base + "\n" + wizard.render_config(window), encoding="utf-8")
    return path


def countdown(notify: Callable[[str], None], seconds: int = 5) -> None:
    """Give the user time to alt-tab into the game before capture starts."""
    for remaining in range(seconds, 0, -1):
        notify(f"  Switching to the game in {remaining}...")
        time.sleep(1)
