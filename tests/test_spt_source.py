"""SPT data source: parsing, the trader-payout formula, and source fallback.

The payout formula is verified against SPT's own TraderHelper.ts, which states
the coefficient is inverted. Getting this backwards silently doubles or halves
every profit estimate, so it gets an explicit test.
"""

from __future__ import annotations

import json

import pytest

from flea_bot.scraper.base import PriceSourceError, build_source, fetch_with_fallback
from flea_bot.scraper.models import ItemPrice
from flea_bot.scraper.spt import TRADER_IDS, SPTDataError, SPTDataSource

# Two items: a medkit (Therapist buys) and a rifle (she doesn't).
MEDKIT = "medkit0000000000000000001"
RIFLE = "rifle00000000000000000001"
MED_CATEGORY = "5448f3a64bdc2d60728b456a"
WEAPON_CATEGORY = "5422acb9af1c889c16000029"


def write_db(root, *, with_items=True):
    """Lay out a miniature SPT database on disk."""
    templates = root / "templates"
    templates.mkdir(parents=True)
    (root / "locales" / "global").mkdir(parents=True)

    (templates / "prices.json").write_text(
        json.dumps({MEDKIT: 30_000, RIFLE: 90_000, "unnamed000": 500})
    )
    (templates / "handbook.json").write_text(
        json.dumps(
            {
                "Categories": [],
                "Items": [
                    {"Id": MEDKIT, "ParentId": "hb1", "Price": 20_000},
                    {"Id": RIFLE, "ParentId": "hb2", "Price": 50_000},
                ],
            }
        )
    )
    (root / "locales" / "global" / "en.json").write_text(
        json.dumps(
            {
                f"{MEDKIT} Name": "Salewa first aid kit",
                f"{MEDKIT} ShortName": "Salewa",
                f"{RIFLE} Name": "Colt M4A1 5.56x45 assault rifle",
                f"{RIFLE} ShortName": "M4A1",
            }
        )
    )
    if with_items:
        (templates / "items.json").write_text(
            json.dumps(
                {
                    MEDKIT: {"_parent": MED_CATEGORY},
                    RIFLE: {"_parent": WEAPON_CATEGORY},
                    MED_CATEGORY: {"_parent": ""},
                    WEAPON_CATEGORY: {"_parent": ""},
                }
            )
        )

    # Therapist buys medical only; Prapor buys weapons only.
    for name, categories in (("Therapist", [MED_CATEGORY]), ("Prapor", [WEAPON_CATEGORY])):
        tdir = root / "traders" / TRADER_IDS[name]
        tdir.mkdir(parents=True)
        coef = 37 if name == "Therapist" else 50
        (tdir / "base.json").write_text(
            json.dumps(
                {
                    "currency": "RUB",
                    "loyaltyLevels": [{"buy_price_coef": coef}],
                    "items_buy": {"category": categories, "id_list": []},
                    "items_buy_prohibited": {"category": [], "id_list": []},
                }
            )
        )
    return root


@pytest.fixture
def spt_root(tmp_path, config):
    root = write_db(tmp_path / "spt_db")
    config.prices.spt_install_path = str(root)
    return root


class TestLocalSource:
    def test_finds_install_from_configured_path(self, spt_root, config):
        assert SPTDataSource.find_install(config) == spt_root

    def test_reports_local_name(self, spt_root, config):
        assert SPTDataSource(config).name == "spt"

    def test_parses_prices_and_names(self, spt_root, config):
        items = {i.item_name: i for i in SPTDataSource(config).fetch_all()}
        assert set(items) == {"Salewa first aid kit", "Colt M4A1 5.56x45 assault rifle"}
        assert items["Salewa first aid kit"].price == 30_000
        assert items["Salewa first aid kit"].short_name == "Salewa"

    def test_skips_items_with_no_english_name(self, spt_root, config):
        # "unnamed000" is priced but absent from the locale file.
        assert all(i.item_id != "unnamed000" for i in SPTDataSource(config).fetch_all())

    def test_records_handbook_price_as_base(self, spt_root, config):
        salewa = next(
            i for i in SPTDataSource(config).fetch_all() if i.short_name == "Salewa"
        )
        assert salewa.base_price == 20_000


class TestTraderPayoutFormula:
    """trader_pays = handbook_price * (100 - buy_price_coef) / 100."""

    def test_therapist_coefficient_is_inverted(self, spt_root, config):
        salewa = next(
            i for i in SPTDataSource(config).fetch_all() if i.short_name == "Salewa"
        )
        best = salewa.best_trader()
        assert best is not None
        assert best.vendor == "Therapist"
        # coef 37 -> pays 63% of the 20,000 handbook price.
        assert best.price_rub == 12_600

    def test_prapor_coefficient(self, spt_root, config):
        rifle = next(i for i in SPTDataSource(config).fetch_all() if i.short_name == "M4A1")
        best = rifle.best_trader()
        # coef 50 -> pays 50% of 50,000.
        assert best.vendor == "Prapor"
        assert best.price_rub == 25_000

    def test_payout_is_not_the_raw_coefficient(self, spt_root, config):
        """Guards against reading the coefficient as the payout percentage."""
        salewa = next(
            i for i in SPTDataSource(config).fetch_all() if i.short_name == "Salewa"
        )
        assert salewa.best_trader().price_rub != 7_400  # 37% of 20,000

    def test_trader_eligibility_is_respected_locally(self, spt_root, config):
        """Therapist must not be offered as a buyer for a rifle."""
        items = {i.short_name: i for i in SPTDataSource(config).fetch_all()}
        rifle_vendors = {v.vendor for v in items["M4A1"].sell_for}
        salewa_vendors = {v.vendor for v in items["Salewa"].sell_for}
        assert "Therapist" not in rifle_vendors
        assert "Prapor" not in salewa_vendors

    def test_flea_vendor_always_present(self, spt_root, config):
        for item in SPTDataSource(config).fetch_all():
            assert any(v.is_flea for v in item.sell_for)


class TestMirrorFallback:
    def test_missing_items_json_disables_eligibility_filter(self, tmp_path, config):
        """Without items.json every trader becomes a candidate (upper bound)."""
        root = write_db(tmp_path / "no_items", with_items=False)
        config.prices.spt_install_path = str(root)

        items = {i.short_name: i for i in SPTDataSource(config).fetch_all()}
        rifle_vendors = {v.vendor for v in items["M4A1"].sell_for}
        # Therapist can't actually buy a rifle, but we can't know that here.
        assert "Therapist" in rifle_vendors

    def test_local_disabled_without_install_raises(self, config, tmp_path):
        config.prices.spt_install_path = str(tmp_path / "nonexistent")
        with pytest.raises(SPTDataError, match="No local SPT install"):
            SPTDataSource(config, allow_mirror=False)

    def test_find_install_returns_none_when_absent(self, config, tmp_path):
        config.prices.spt_install_path = str(tmp_path / "nope")
        assert SPTDataSource.find_install(config) is None


class TestSourceSelection:
    def test_explicit_tarkov_dev(self, config):
        config.prices.source = "tarkov.dev"
        assert build_source(config).name == "tarkov.dev"

    def test_explicit_spt(self, spt_root, config):
        config.prices.source = "spt"
        assert build_source(config).name == "spt"

    def test_auto_prefers_local_install(self, spt_root, config):
        config.prices.source = "auto"
        assert build_source(config).name == "spt"

    def test_auto_falls_back_to_mirror(self, config, tmp_path):
        config.prices.source = "auto"
        config.prices.spt_install_path = str(tmp_path / "nope")
        assert build_source(config).name == "spt-mirror"

    def test_unknown_source_rejected(self, config):
        config.prices.source = "nonsense"
        with pytest.raises(PriceSourceError, match="Unknown"):
            build_source(config)


class TestFetchWithFallback:
    def test_uses_first_working_source(self, spt_root, config):
        config.prices.source = "spt"
        items, source = fetch_with_fallback(config)
        assert source == "spt"
        assert len(items) == 2
        assert all(isinstance(i, ItemPrice) for i in items)

    def test_falls_through_when_a_source_fails(self, spt_root, config, monkeypatch):
        """A dead tarkov.dev must not stop SPT data being used."""
        config.prices.source = "tarkov.dev"

        from flea_bot.scraper import client as client_mod

        def boom(self):
            raise RuntimeError("GraphQL server unavailable")

        monkeypatch.setattr(client_mod.TarkovPriceClient, "fetch_all", boom)

        items, source = fetch_with_fallback(config)
        assert source == "spt", "should degrade to SPT rather than fail"
        assert len(items) == 2

    def test_raises_only_when_everything_fails(self, config, tmp_path, monkeypatch):
        config.prices.source = "auto"
        config.prices.spt_install_path = str(tmp_path / "nope")

        from flea_bot.scraper import client as client_mod
        from flea_bot.scraper import spt as spt_mod

        monkeypatch.setattr(
            spt_mod.SPTDataSource, "fetch_all",
            lambda self: (_ for _ in ()).throw(RuntimeError("mirror down")),
        )
        monkeypatch.setattr(
            client_mod.TarkovPriceClient, "fetch_all",
            lambda self: (_ for _ in ()).throw(RuntimeError("api down")),
        )

        with pytest.raises(PriceSourceError, match="Every price source failed"):
            fetch_with_fallback(config)


class TestSourceIsolationInHistory:
    """SPT and live prices are different economies; stats must not mix them."""

    def test_stats_filtered_by_source(self, repo):
        from tests.conftest import make_item

        repo.insert_snapshots([make_item("Bolts", 1_000)], source="spt")
        repo.insert_snapshots([make_item("Bolts", 99_000)], source="tarkov.dev")

        assert repo.stats("bolts", source="spt").mean == 1_000
        assert repo.stats("bolts", source="tarkov.dev").mean == 99_000
        # Unfiltered deliberately still averages everything.
        assert repo.stats("bolts").samples == 2

    def test_stats_for_all_filtered_by_source(self, repo):
        from tests.conftest import make_item

        repo.insert_snapshots([make_item("Bolts", 1_000)], source="spt")
        repo.insert_snapshots([make_item("Bolts", 99_000)], source="tarkov.dev")

        spt_stats = repo.stats_for_all(source="spt")
        assert spt_stats["bolts"].samples == 1
        assert spt_stats["bolts"].mean == 1_000
