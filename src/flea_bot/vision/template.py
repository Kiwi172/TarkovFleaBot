"""Template matching: find a known UI element on screen.

Uses ``cv2.matchTemplate`` with ``TM_CCOEFF_NORMED``, which yields a score in
roughly [-1, 1] where 1 is a perfect match — that normalisation is what makes a
single configurable confidence threshold meaningful across different templates.

Templates are cached in memory: reloading a PNG from disk on every frame of a
polling loop is pure overhead.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from flea_bot.config import Config, Region, get_config
from flea_bot.logging_setup import get_logger
from flea_bot.vision.capture import ScreenCapture

log = get_logger("vision")


class TemplateNotFoundError(FileNotFoundError):
    """The reference image is missing from disk."""


@dataclass(frozen=True, slots=True)
class Match:
    """A located UI element, in absolute screen coordinates."""

    x: int
    y: int
    width: int
    height: int
    confidence: float

    @property
    def center(self) -> tuple[int, int]:
        """Click target — the middle of the matched box."""
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def region(self) -> Region:
        return (self.x, self.y, self.width, self.height)

    def __repr__(self) -> str:
        return f"<Match ({self.x},{self.y}) {self.width}x{self.height} conf={self.confidence:.3f}>"


@lru_cache(maxsize=64)
def load_template(path: str) -> np.ndarray:
    """Load a reference image as BGR. Cached by path."""
    p = Path(path)
    if not p.is_file():
        raise TemplateNotFoundError(
            f"Template image not found: {p}\n"
            f"Capture it from your own client first, e.g.:\n"
            f"  flea-bot snip --name {p.stem} --region <left>,<top>,<width>,<height>"
        )
    image = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if image is None:
        raise TemplateNotFoundError(f"Could not decode template image: {p}")
    return image


def clear_template_cache() -> None:
    load_template.cache_clear()


def match_template(
    haystack: np.ndarray,
    needle: np.ndarray,
    *,
    threshold: float = 0.85,
    offset: tuple[int, int] = (0, 0),
    grayscale: bool = True,
) -> Match | None:
    """Find the single best occurrence of ``needle`` in ``haystack``.

    ``offset`` is added to the result so that searching inside a cropped region
    still returns absolute screen coordinates. Returns None below threshold.
    """
    if needle.shape[0] > haystack.shape[0] or needle.shape[1] > haystack.shape[1]:
        log.warning(
            "Template {}x{} is larger than the search area {}x{} — cannot match.",
            needle.shape[1],
            needle.shape[0],
            haystack.shape[1],
            haystack.shape[0],
        )
        return None

    if grayscale:
        # Tarkov's UI is low-saturation; luminance carries the signal and this
        # is ~3x faster than matching all three channels.
        haystack = cv2.cvtColor(haystack, cv2.COLOR_BGR2GRAY)
        needle = cv2.cvtColor(needle, cv2.COLOR_BGR2GRAY)

    result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
    _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)

    if max_val < threshold:
        log.debug("Best match {:.3f} below threshold {:.3f}", max_val, threshold)
        return None

    h, w = needle.shape[:2]
    return Match(
        x=int(max_loc[0]) + offset[0],
        y=int(max_loc[1]) + offset[1],
        width=int(w),
        height=int(h),
        confidence=float(max_val),
    )


def match_template_all(
    haystack: np.ndarray,
    needle: np.ndarray,
    *,
    threshold: float = 0.85,
    offset: tuple[int, int] = (0, 0),
    grayscale: bool = True,
    max_results: int = 50,
) -> list[Match]:
    """Find every occurrence, de-duplicated by non-maximum suppression.

    Used for repeated elements — the rows of a flea market offer list. Without
    the suppression step you get a dozen overlapping hits per real row.
    """
    if grayscale:
        haystack = cv2.cvtColor(haystack, cv2.COLOR_BGR2GRAY)
        needle = cv2.cvtColor(needle, cv2.COLOR_BGR2GRAY)

    result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(result >= threshold)
    h, w = needle.shape[:2]

    candidates = sorted(
        (Match(int(x) + offset[0], int(y) + offset[1], w, h, float(result[y, x]))
         for x, y in zip(xs, ys, strict=True)),  # np.where guarantees equal length
        key=lambda m: m.confidence,
        reverse=True,
    )

    kept: list[Match] = []
    for cand in candidates:
        if len(kept) >= max_results:
            break
        # Suppress anything overlapping an already-accepted, higher-scoring hit.
        if any(_iou(cand, k) > 0.3 for k in kept):
            continue
        kept.append(cand)

    kept.sort(key=lambda m: (m.y, m.x))  # reading order
    log.debug("Found {} match(es) above {:.3f}", len(kept), threshold)
    return kept


def _iou(a: Match, b: Match) -> float:
    """Intersection-over-union of two boxes."""
    x1, y1 = max(a.x, b.x), max(a.y, b.y)
    x2 = min(a.x + a.width, b.x + b.width)
    y2 = min(a.y + a.height, b.y + b.height)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    union = a.width * a.height + b.width * b.height - inter
    return inter / union


class TemplateMatcher:
    """Config-aware façade over the functions above.

    Resolves template names from ``[window.templates]`` and searches either the
    whole game window or a named region.
    """

    def __init__(
        self,
        config: Config | None = None,
        capture: ScreenCapture | None = None,
    ) -> None:
        self.config = config or get_config()
        self.capture = capture or ScreenCapture(self.config)

    def find(
        self,
        template_name: str,
        *,
        region: str | Region | None = None,
        threshold: float | None = None,
        screenshot: np.ndarray | None = None,
    ) -> Match | None:
        """Locate a named template. Returns absolute screen coords, or None."""
        thresh = threshold if threshold is not None else self.config.thresholds.template_confidence
        needle = load_template(str(self.config.window.template(template_name)))

        box = self._resolve_region(region)
        haystack = self.capture.grab(box) if screenshot is None else screenshot
        offset = (box[0], box[1]) if box else (0, 0)

        match = match_template(haystack, needle, threshold=thresh, offset=offset)
        if match is None:
            log.debug("Template {!r} not found (threshold {:.2f})", template_name, thresh)
        else:
            log.debug(
                "Template {!r} at {} conf={:.3f}",
                template_name,
                match.center,
                match.confidence,
            )
        return match

    def find_all(
        self,
        template_name: str,
        *,
        region: str | Region | None = None,
        threshold: float | None = None,
    ) -> list[Match]:
        thresh = threshold if threshold is not None else self.config.thresholds.template_confidence
        needle = load_template(str(self.config.window.template(template_name)))
        box = self._resolve_region(region)
        haystack = self.capture.grab(box)
        offset = (box[0], box[1]) if box else (0, 0)
        return match_template_all(haystack, needle, threshold=thresh, offset=offset)

    def exists(self, template_name: str, **kwargs) -> bool:
        """True if the element is on screen — the FSM's main state predicate."""
        return self.find(template_name, **kwargs) is not None

    def wait_for(
        self,
        template_name: str,
        *,
        timeout: float = 10.0,
        poll_interval: float = 0.25,
        region: str | Region | None = None,
        threshold: float | None = None,
    ) -> Match | None:
        """Poll until the element appears or ``timeout`` elapses."""
        import time  # noqa: PLC0415

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = self.find(template_name, region=region, threshold=threshold)
            if match is not None:
                return match
            time.sleep(poll_interval)
        log.warning("Timed out after {:.1f}s waiting for {!r}", timeout, template_name)
        return None

    def _resolve_region(self, region: str | Region | None) -> Region | None:
        if region is None:
            return None
        if isinstance(region, str):
            return self.config.window.region(region)
        return region
