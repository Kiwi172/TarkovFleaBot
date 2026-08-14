"""Price data straight from SPT's own server database.

This is the best source available for SPT, because it *is* the table your
server prices the flea market from. Three files carry everything needed:

``templates/prices.json``
    ``{template_id: flea_price}`` — the flea market price table.

``templates/handbook.json``
    ``{"Items": [{"Id", "ParentId", "Price"}]}`` — the handbook base price,
    which is what trader payouts are derived from.

``locales/global/en.json``
    A flat locale dict; item names live under ``"<template_id> Name"``.

### How trader payouts are computed

From SPT's own ``TraderHelper.ts``, which is explicit about the inversion::

    // buy_price_coef is the inverse percentage,
    // must subtract from 100 to get proper buyback percent
    const pct = 100 - traderBase.loyaltyLevels[0].buy_price_coef;
    const price = round(getPercentOfValue(pct, itemHandbookPrice));

So ``trader_pays = handbook_price * (100 - buy_price_coef) / 100``. Therapist's
coefficient of 37 means she pays **63%** of handbook, not 37%.

### The one thing the mirror can't tell you

Which traders will buy a given item is defined by ``items_buy.category`` in
each trader's ``base.json``, and those are *item template* category ids. The
parent chain that resolves them lives in ``templates/items.json``, an 18 MB
Git-LFS file that the GitHub raw/media endpoints do not serve.

Consequently:

* **Local install** — full fidelity. Trader eligibility is resolved exactly.
* **Mirror** — prices and names are exact, but eligibility is unknown, so the
  payout is the best across all traders and may name a trader who would refuse
  the item. Treat mirror trader prices as an upper bound, and pin anything you
  actually trade in ``config/trader_prices.yaml``.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from flea_bot.config import Config, get_config
from flea_bot.logging_setup import get_logger
from flea_bot.scraper.models import ItemPrice, VendorPrice

log = get_logger("scraper")

# Trader display name -> template id. Fence is excluded: he pays a pittance and
# his rates are dynamic, so including him only adds noise.
TRADER_IDS: dict[str, str] = {
    "Prapor": "54cb50c76803fa8b248b4571",
    "Therapist": "54cb57776803fa99248b456e",
    "Skier": "58330581ace78e27b8b10cee",
    "Peacekeeper": "5935c25fb3acc3127c3d8cd9",
    "Mechanic": "5a7c2eca46aef81a7ca2145d",
    "Ragman": "5ac3b934156ae10c4430e83c",
    "Jaeger": "5c0647fdd443bc2504c2d371",
}

# Where the server database lives inside an SPT install, newest layout first.
DB_SUBPATHS = (
    Path("SPT_Data/Server/database"),
    Path("Aki_Data/Server/database"),
    Path("user/mods"),  # not a db, but its presence identifies an install root
)

DEFAULT_MIRROR = (
    "https://raw.githubusercontent.com/sp-tarkov/server/master/project/assets/database"
)


class SPTDataError(RuntimeError):
    """SPT data could not be read from disk or the mirror."""


def _load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_json_url(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "flea-bot/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode("utf-8"))


class SPTDataSource:
    """Reads prices from a local SPT install, or the SPT repo as a fallback."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        allow_local: bool = True,
        allow_mirror: bool = True,
    ) -> None:
        self.config = config or get_config()
        self._allow_local = allow_local
        self._allow_mirror = allow_mirror
        self.db_root: Path | None = self.find_install(self.config) if allow_local else None

        if self.db_root is None and not allow_mirror:
            raise SPTDataError(
                "No local SPT install found. Set [prices].spt_install_path to your "
                "SPT folder (the one containing SPT_Data/), or use "
                "[prices].source = \"spt-mirror\"."
            )

        self.local = self.db_root is not None
        self.name = "spt" if self.local else "spt-mirror"

    # ------------------------------------------------------------------
    @staticmethod
    def find_install(config: Config) -> Path | None:
        """Locate an SPT server database directory, or None.

        Checks ``[prices].spt_install_path`` first, then a few conventional
        locations so the common case needs no configuration.
        """
        candidates: list[Path] = []
        if config.prices.spt_install_path:
            candidates.append(Path(config.prices.spt_install_path).expanduser())

        candidates += [
            Path.home() / "SPT",
            Path.home() / "spt",
            Path("C:/SPT"),
            Path("C:/Games/SPT"),
        ]

        for root in candidates:
            if not root.exists():
                continue
            # Accept either the install root or the database dir itself.
            if (root / "templates" / "prices.json").is_file():
                return root
            for sub in DB_SUBPATHS:
                db = root / sub
                if (db / "templates" / "prices.json").is_file():
                    return db
        return None

    # ------------------------------------------------------------------
    def _read(self, relative: str) -> Any:
        """Read one database file from disk or the mirror."""
        if self.local and self.db_root is not None:
            path = self.db_root / relative
            if not path.is_file():
                raise SPTDataError(f"Missing SPT data file: {path}")
            return _load_json_file(path)

        if not self._allow_mirror:
            raise SPTDataError(f"Cannot read {relative}: mirror disabled")

        url = f"{self.config.prices.spt_mirror_url.rstrip('/')}/{relative}"
        try:
            return _load_json_url(url, self.config.prices.request_timeout)
        except Exception as exc:
            raise SPTDataError(f"Could not fetch {url}: {exc}") from exc

    # ------------------------------------------------------------------
    def _trader_payouts(self, handbook_price: dict[str, int]) -> dict[str, list[VendorPrice]]:
        """Best trader payout per item template.

        Locally this respects each trader's ``items_buy`` categories. From the
        mirror those cannot be resolved, so every trader is considered a
        candidate and the result is an upper bound.
        """
        item_parents = self._item_parents() if self.local else None
        payouts: dict[str, list[VendorPrice]] = {}

        for trader_name, trader_id in TRADER_IDS.items():
            try:
                base = self._read(f"traders/{trader_id}/base.json")
            except SPTDataError as exc:
                log.debug("Skipping trader {}: {}", trader_name, exc)
                continue

            levels = base.get("loyaltyLevels") or []
            if not levels:
                continue
            coef = levels[0].get("buy_price_coef")
            if coef is None:
                continue
            # See module docstring: the coefficient is inverted.
            buyback_pct = (100 - coef) / 100.0
            if buyback_pct <= 0:
                continue

            buy_categories = set((base.get("items_buy") or {}).get("category") or [])
            banned = set((base.get("items_buy_prohibited") or {}).get("category") or [])

            for tpl, hb_price in handbook_price.items():
                if item_parents is not None:
                    chain = item_parents.get(tpl)
                    if chain is None:
                        continue
                    if banned & chain:
                        continue
                    if buy_categories and not (buy_categories & chain):
                        continue
                price = round(hb_price * buyback_pct)
                if price > 0:
                    payouts.setdefault(tpl, []).append(
                        VendorPrice(vendor=trader_name, price_rub=price)
                    )

        return payouts

    def _item_parents(self) -> dict[str, set[str]] | None:
        """Map template id -> set of ancestor category ids.

        Only possible with a local install; ``items.json`` is an 18 MB LFS file
        the GitHub mirror does not serve.
        """
        try:
            items = self._read("templates/items.json")
        except SPTDataError as exc:
            log.warning("Could not read items.json ({}) — trader eligibility unknown.", exc)
            return None

        if not isinstance(items, dict):
            return None

        direct = {tpl: node.get("_parent") for tpl, node in items.items()}

        def ancestors(tpl: str) -> set[str]:
            out: set[str] = set()
            seen: set[str] = set()
            cur = direct.get(tpl)
            while cur and cur not in seen:
                seen.add(cur)
                out.add(cur)
                cur = direct.get(cur)
            return out

        return {tpl: ancestors(tpl) for tpl in items}

    # ------------------------------------------------------------------
    def fetch_all(self) -> list[ItemPrice]:
        """Build :class:`ItemPrice` records from the SPT database."""
        where = str(self.db_root) if self.local else self.config.prices.spt_mirror_url
        log.info("Reading SPT price data from {} ({})", where, self.name)

        prices: dict[str, int] = self._read("templates/prices.json")
        handbook = self._read("templates/handbook.json")
        locale = self._read("locales/global/en.json")

        handbook_price: dict[str, int] = {
            entry["Id"]: int(entry["Price"])
            for entry in handbook.get("Items", [])
            if entry.get("Price")
        }
        payouts = self._trader_payouts(handbook_price)

        items: list[ItemPrice] = []
        unnamed = 0
        for tpl, flea_price in prices.items():
            if not flea_price or flea_price <= 0:
                continue
            name = locale.get(f"{tpl} Name")
            if not name:
                unnamed += 1
                continue

            sell_for = [VendorPrice(vendor="Flea Market", price_rub=int(flea_price))]
            sell_for.extend(payouts.get(tpl, ()))

            items.append(
                ItemPrice(
                    item_id=tpl,
                    item_name=name,
                    short_name=locale.get(f"{tpl} ShortName", "") or "",
                    price=int(flea_price),
                    quantity=1,
                    base_price=handbook_price.get(tpl),
                    sell_for=tuple(sell_for),
                )
            )

        if unnamed:
            log.debug("Skipped {} priced template(s) with no English name.", unnamed)

        if not self.local:
            log.warning(
                "Using the SPT mirror: trader payouts are an UPPER BOUND because "
                "trader eligibility can't be resolved without a local install. "
                "Pin anything you trade in config/trader_prices.yaml."
            )

        log.info("Loaded {} priced item(s) from SPT data.", len(items))
        return items

    def close(self) -> None:
        """Nothing to release — files and HTTP reads are one-shot."""
