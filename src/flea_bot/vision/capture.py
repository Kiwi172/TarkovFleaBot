"""Screen capture via mss.

:class:`ScreenCapture` holds one mss instance for the process — creating a new
one per grab is slow and leaks X11/GDI handles under a tight polling loop.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

import numpy as np

from flea_bot.config import Config, Region, get_config
from flea_bot.logging_setup import get_logger

log = get_logger("vision")


class ScreenCapture:
    """Grabs BGR frames of the screen or a sub-region.

    Returns OpenCV-native BGR arrays (mss gives BGRA), so output drops straight
    into ``cv2`` calls without another conversion.
    """

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or get_config()
        self._sct = None  # lazily created; mss objects are not picklable

    def _ensure(self):
        if self._sct is None:
            import mss  # noqa: PLC0415 - heavy, and needs a display

            self._sct = mss.mss()
        return self._sct

    def __enter__(self) -> ScreenCapture:
        self._ensure()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    # ------------------------------------------------------------------
    def grab(self, region: Region | None = None) -> np.ndarray:
        """Capture a region (left, top, width, height), or the game window.

        Coordinates are absolute screen pixels.
        """
        sct = self._ensure()
        if region is None:
            w = self.config.window
            box = {"left": w.left, "top": w.top, "width": w.width, "height": w.height}
        else:
            left, top, width, height = region
            box = {"left": left, "top": top, "width": width, "height": height}

        raw = sct.grab(box)
        # mss returns BGRA; drop alpha for cv2.
        frame = np.asarray(raw, dtype=np.uint8)[:, :, :3]
        return np.ascontiguousarray(frame)

    def grab_region(self, name: str) -> np.ndarray:
        """Capture a region named in ``[window.regions]``."""
        return self.grab(self.config.window.region(name))

    def grab_monitor(self) -> np.ndarray:
        """Capture the whole configured monitor, ignoring window geometry."""
        sct = self._ensure()
        monitor = sct.monitors[self.config.window.monitor]
        frame = np.asarray(sct.grab(monitor), dtype=np.uint8)[:, :, :3]
        return np.ascontiguousarray(frame)

    def save(self, frame: np.ndarray, path: Path | str) -> Path:
        """Write a frame to disk — used by the ``snip`` command and debugging."""
        import cv2  # noqa: PLC0415

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(target), frame)
        log.debug("Saved capture to {}", target)
        return target

    def debug_dump(self, frame: np.ndarray, label: str) -> Path:
        """Save a timestamped frame under ``data/debug_captures/``.

        Call this whenever vision fails — a failed match is nearly impossible
        to diagnose without seeing what the bot actually saw.
        """
        from datetime import datetime  # noqa: PLC0415

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        return self.save(
            frame, self.config.data_dir / "debug_captures" / f"{stamp}_{label}.png"
        )
