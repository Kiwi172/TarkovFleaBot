"""Tkinter dashboard.

``app`` is imported lazily by :func:`launch` so that importing this package on
a headless machine (CI, a server) doesn't fail on a missing display.
"""

from flea_bot.gui.controller import BotController, BotEvent, BotState, EventType

__all__ = ["BotController", "BotEvent", "BotState", "EventType", "launch"]


def launch(config=None) -> None:
    """Open the dashboard window."""
    from flea_bot.gui.app import launch as _launch

    _launch(config)
