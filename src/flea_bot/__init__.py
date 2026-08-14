"""flea-bot — flea market price analysis and UI automation for SPT.

Built for SPT (SPT-AKI), the offline single-player Tarkov server emulator.
See README.md for scope and limitations.
"""

__version__ = "0.1.0"

from flea_bot.config import Config, get_config, load_config

__all__ = ["Config", "__version__", "get_config", "load_config"]
