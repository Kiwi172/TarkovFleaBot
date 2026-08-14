"""The :class:`PriceSource` protocol and source-selection logic.

There is more than one way to learn what an item costs, and they are not
equally good for SPT:

``spt``
    Your own SPT server's data files. This is *the actual price table your
    game uses*, so it is exact by construction. Best source, but only
    available on the machine SPT is installed on.

``spt-mirror``
    The same files from the SPT server repo on GitHub, for when you're not on
    the SPT machine. Prices and names are exact; **trader eligibility is not**
    (see :mod:`flea_bot.scraper.spt`).

``tarkov.dev``
    Live Tarkov community data. Useful as a cross-reference and for items your
    SPT version doesn't price, but it describes a different economy.

Sources are interchangeable — everything downstream consumes
:class:`~flea_bot.scraper.models.ItemPrice`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from flea_bot.config import Config, get_config
from flea_bot.logging_setup import get_logger
from flea_bot.scraper.models import ItemPrice

log = get_logger("scraper")


class PriceSourceError(RuntimeError):
    """A price source could not be read."""


@runtime_checkable
class PriceSource(Protocol):
    """Anything that can produce a list of priced items."""

    name: str

    def fetch_all(self) -> list[ItemPrice]:
        """Return every item this source knows a price for."""
        ...

    def close(self) -> None:
        """Release any held resources."""
        ...


def build_source(config: Config | None = None, *, name: str | None = None) -> PriceSource:
    """Construct the configured price source.

    ``name`` (or ``[prices].source``) may be ``auto``, ``spt``, ``spt-mirror``
    or ``tarkov.dev``. ``auto`` prefers a local SPT install, then the mirror,
    then tarkov.dev — i.e. most-accurate-first, with each fallback logged so
    you always know which economy the numbers describe.
    """
    cfg = config or get_config()
    choice = (name or cfg.prices.source or "auto").strip().lower()

    # Imported here to keep httpx/json loading off the import path of callers
    # that only want one of them.
    from flea_bot.scraper.client import TarkovPriceClient
    from flea_bot.scraper.spt import SPTDataSource

    if choice == "tarkov.dev":
        return TarkovPriceClient(cfg)
    if choice == "spt":
        return SPTDataSource(cfg, allow_mirror=False)
    if choice == "spt-mirror":
        return SPTDataSource(cfg, allow_local=False)
    if choice != "auto":
        raise PriceSourceError(
            f"Unknown [prices].source {choice!r}. "
            f"Expected one of: auto, spt, spt-mirror, tarkov.dev"
        )

    # auto
    if SPTDataSource.find_install(cfg) is not None:
        return SPTDataSource(cfg)
    log.info("No local SPT install found — using the SPT data mirror.")
    return SPTDataSource(cfg, allow_local=False)


def fetch_with_fallback(config: Config | None = None) -> tuple[list[ItemPrice], str]:
    """Fetch prices, falling back through sources on failure.

    Returns ``(items, source_name)``. Raises :class:`PriceSourceError` only if
    every candidate fails — a single source being down (as tarkov.dev
    regularly is) should not stop the tool working.
    """
    cfg = config or get_config()
    configured = (cfg.prices.source or "auto").strip().lower()

    if configured == "auto":
        candidates = ["spt", "spt-mirror", "tarkov.dev"]
    else:
        # An explicit choice is honoured first, then we still degrade rather
        # than fail outright.
        candidates = [configured] + [
            c for c in ("spt", "spt-mirror", "tarkov.dev") if c != configured
        ]

    failures: list[str] = []
    for candidate in candidates:
        try:
            source = build_source(cfg, name=candidate)
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")
            continue

        try:
            items = source.fetch_all()
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")
            log.warning("Price source {!r} failed: {}", candidate, exc)
            continue
        finally:
            source.close()

        if items:
            if candidate != candidates[0]:
                log.warning(
                    "Fell back to price source {!r} after: {}",
                    candidate,
                    "; ".join(failures),
                )
            return items, source.name
        failures.append(f"{candidate}: returned no items")

    raise PriceSourceError(
        "Every price source failed:\n  " + "\n  ".join(failures)
    )
