"""Platform backends for mouse/keyboard dispatch.

Why an abstraction rather than importing pydirectinput directly:

* **pydirectinput is Windows-only.** It writes raw scancodes through the Win32
  ``SendInput`` API, which is what DirectInput games read. On Linux (where a
  lot of SPT development happens, and where SPT itself runs under Proton) the
  module won't even import.
* **Dry-run and tests need a no-op sink** that records intent without moving a
  real cursor.

:func:`get_backend` picks automatically; the controller never cares which one
it got.
"""

from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from flea_bot.logging_setup import get_logger

log = get_logger("input")


class InputBackendError(RuntimeError):
    """No usable input backend on this platform."""


class InputBackend(ABC):
    """Minimal surface the controller needs."""

    name: str = "abstract"

    @abstractmethod
    def move_to(self, x: int, y: int) -> None: ...

    @abstractmethod
    def mouse_down(self, button: str = "left") -> None: ...

    @abstractmethod
    def mouse_up(self, button: str = "left") -> None: ...

    @abstractmethod
    def click(self, button: str = "left") -> None: ...

    @abstractmethod
    def key_down(self, key: str) -> None: ...

    @abstractmethod
    def key_up(self, key: str) -> None: ...

    @abstractmethod
    def write(self, text: str) -> None: ...

    @abstractmethod
    def scroll(self, clicks: int) -> None: ...

    @abstractmethod
    def position(self) -> tuple[int, int]: ...


class PyDirectInputBackend(InputBackend):
    """Windows. Uses SendInput scancodes, which DirectInput titles honour."""

    name = "pydirectinput"

    def __init__(self) -> None:
        import pydirectinput  # noqa: PLC0415

        self._pdi = pydirectinput
        # We do our own pacing in the controller; the library's global sleep
        # would stack on top of it and make everything sluggish.
        pydirectinput.PAUSE = 0.0
        # Corner-of-screen abort is pyautogui's idea of a panic button. Ours is
        # the kill hotkey in flea_bot.safety, which works without the cursor.
        pydirectinput.FAILSAFE = False

    def move_to(self, x: int, y: int) -> None:
        self._pdi.moveTo(x, y, _pause=False)

    def mouse_down(self, button: str = "left") -> None:
        self._pdi.mouseDown(button=button, _pause=False)

    def mouse_up(self, button: str = "left") -> None:
        self._pdi.mouseUp(button=button, _pause=False)

    def click(self, button: str = "left") -> None:
        self._pdi.click(button=button, _pause=False)

    def key_down(self, key: str) -> None:
        self._pdi.keyDown(key, _pause=False)

    def key_up(self, key: str) -> None:
        self._pdi.keyUp(key, _pause=False)

    def write(self, text: str) -> None:
        self._pdi.write(text, _pause=False)

    def scroll(self, clicks: int) -> None:
        self._pdi.scroll(clicks, _pause=False)

    def position(self) -> tuple[int, int]:
        return tuple(self._pdi.position())  # type: ignore[return-value]


class PyAutoGUIBackend(InputBackend):
    """Linux/macOS fallback.

    Caveat: pyautogui synthesises events through XTEST, which many games
    ignore because it doesn't produce real DirectInput. Fine for building and
    debugging the pipeline against a windowed client; if your SPT client
    doesn't respond to it, run the bot on Windows with pydirectinput.
    """

    name = "pyautogui"

    def __init__(self) -> None:
        import pyautogui  # noqa: PLC0415

        self._gui = pyautogui
        pyautogui.PAUSE = 0.0
        pyautogui.FAILSAFE = False

    def move_to(self, x: int, y: int) -> None:
        self._gui.moveTo(x, y)

    def mouse_down(self, button: str = "left") -> None:
        self._gui.mouseDown(button=button)

    def mouse_up(self, button: str = "left") -> None:
        self._gui.mouseUp(button=button)

    def click(self, button: str = "left") -> None:
        self._gui.click(button=button)

    def key_down(self, key: str) -> None:
        self._gui.keyDown(key)

    def key_up(self, key: str) -> None:
        self._gui.keyUp(key)

    def write(self, text: str) -> None:
        self._gui.write(text)

    def scroll(self, clicks: int) -> None:
        self._gui.scroll(clicks)

    def position(self) -> tuple[int, int]:
        pos = self._gui.position()
        return (int(pos[0]), int(pos[1]))


@dataclass
class NullBackend(InputBackend):
    """Records calls instead of dispatching them. Used by dry-run and tests."""

    name: str = "null"
    calls: list[tuple[str, tuple]] = field(default_factory=list)
    _pos: tuple[int, int] = (0, 0)

    def _record(self, action: str, *args: object) -> None:
        self.calls.append((action, args))

    def move_to(self, x: int, y: int) -> None:
        self._record("move_to", x, y)
        self._pos = (x, y)

    def mouse_down(self, button: str = "left") -> None:
        self._record("mouse_down", button)

    def mouse_up(self, button: str = "left") -> None:
        self._record("mouse_up", button)

    def click(self, button: str = "left") -> None:
        self._record("click", button)

    def key_down(self, key: str) -> None:
        self._record("key_down", key)

    def key_up(self, key: str) -> None:
        self._record("key_up", key)

    def write(self, text: str) -> None:
        self._record("write", text)

    def scroll(self, clicks: int) -> None:
        self._record("scroll", clicks)

    def position(self) -> tuple[int, int]:
        return self._pos


def get_backend(*, dry_run: bool = False, prefer: str | None = None) -> InputBackend:
    """Pick an input backend.

    Dry-run always gets :class:`NullBackend` — that guarantee is the whole
    point of dry-run, so it is checked before anything platform-specific.
    """
    if dry_run:
        log.info("Input backend: null (dry-run — no input will be dispatched)")
        return NullBackend()

    candidates: list[type[InputBackend]]
    if prefer == "pydirectinput":
        candidates = [PyDirectInputBackend]
    elif prefer == "pyautogui":
        candidates = [PyAutoGUIBackend]
    elif prefer == "null":
        return NullBackend()
    elif platform.system() == "Windows":
        candidates = [PyDirectInputBackend, PyAutoGUIBackend]
    else:
        candidates = [PyAutoGUIBackend, PyDirectInputBackend]

    errors: list[str] = []
    for cls in candidates:
        try:
            backend = cls()
        except Exception as exc:
            errors.append(f"{cls.name}: {exc}")
            continue
        log.info("Input backend: {}", backend.name)
        if backend.name == "pyautogui" and platform.system() != "Windows":
            log.warning(
                "Using pyautogui on {}. Games often ignore XTEST-synthesised "
                "input; if the client doesn't respond, run on Windows.",
                platform.system(),
            )
        return backend

    raise InputBackendError(
        "No input backend available. Tried:\n  " + "\n  ".join(errors)
    )
