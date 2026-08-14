"""Shared fixtures. Nothing here touches the network, a screen, or a game."""

from __future__ import annotations

import pytest

from flea_bot.config import Config
from flea_bot.database.repository import PriceRepository
from flea_bot.scraper.models import ItemPrice, VendorPrice


@pytest.fixture
def config(tmp_path) -> Config:
    """Config with defaults, dry-run on, and data isolated to tmp_path."""
    cfg = Config()
    cfg.general.dry_run = True
    cfg.general.data_dir = tmp_path / "data"
    cfg.window.regions = {
        "search_box": (640, 180, 400, 32),
        "offer_list": (420, 240, 1080, 640),
        "first_offer_price": (1180, 250, 140, 28),
        "first_offer_quantity": (1330, 250, 90, 28),
        "player_balance": (1560, 60, 200, 30),
    }
    return cfg


@pytest.fixture
def repo(config) -> PriceRepository:
    r = PriceRepository(config, db_path=":memory:")
    yield r
    r.dispose()


def make_item(
    name: str = "Bottle of water",
    price: int = 10_000,
    trader_price: int = 15_000,
    *,
    item_id: str | None = None,
    width: int = 1,
    height: int = 1,
    trader: str = "Therapist",
) -> ItemPrice:
    """Build an ItemPrice with a flea and a trader sell entry."""
    return ItemPrice(
        item_id=item_id or name.lower().replace(" ", "-"),
        item_name=name,
        short_name=name[:6],
        price=price,
        quantity=1,
        avg_24h=price,
        width=width,
        height=height,
        sell_for=(
            VendorPrice(vendor="Flea Market", price_rub=price),
            VendorPrice(vendor=trader, price_rub=trader_price),
        ),
    )


@pytest.fixture
def item_factory():
    return make_item
