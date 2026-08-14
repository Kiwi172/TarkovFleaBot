"""Trader reference, margin maths, and the ranking engine."""

from __future__ import annotations

import textwrap

import pytest

from flea_bot.decision.ranking import RejectReason, SortKey, rank_items
from flea_bot.traders.prices import TraderPriceBook, normalise, profit_margin
from tests.conftest import make_item


@pytest.fixture
def book_file(tmp_path):
    path = tmp_path / "trader_prices.yaml"
    path.write_text(
        textwrap.dedent(
            """
            version: 1
            items:
              "Bottle of water":
                trader: Therapist
                price: 25000
                notes: "override"
              "Bolts": 4000
              "Broken thing":
                trader: Prapor
            blacklist:
              - "Dogtag BEAR"
            """
        ).strip()
    )
    return path


class TestPriceBook:
    def test_loads_mapping_and_shorthand_forms(self, book_file, config):
        book = TraderPriceBook.load(book_file, config=config)
        assert len(book) == 2, "entry without a price must be skipped"
        assert book.override_for("Bottle of water").price == 25_000
        assert book.override_for("Bolts").price == 4_000

    def test_lookup_is_case_and_whitespace_insensitive(self, book_file, config):
        book = TraderPriceBook.load(book_file, config=config)
        assert book.override_for("  BOTTLE   OF  WATER ").price == 25_000

    def test_blacklist_blocks_resolution(self, book_file, config):
        book = TraderPriceBook.load(book_file, config=config)
        assert book.is_blacklisted("dogtag bear")
        assert book.resolve(make_item("Dogtag BEAR", 1_000, 9_999)) is None

    def test_missing_file_yields_empty_book(self, tmp_path, config):
        book = TraderPriceBook.load(tmp_path / "nope.yaml", config=config)
        assert len(book) == 0

    def test_override_beats_api_price(self, book_file, config):
        book = TraderPriceBook.load(book_file, config=config)
        # API says the trader pays 15k; the override says 25k.
        item = make_item("Bottle of water", 10_000, 15_000)
        assert book.resolve(item).price == 25_000
        assert book.resolve(item).source == "override"

    def test_falls_back_to_api_when_no_override(self, book_file, config):
        book = TraderPriceBook.load(book_file, config=config)
        resolved = book.resolve(make_item("Salewa", 10_000, 19_000))
        assert resolved.price == 19_000
        assert resolved.source == "api"

    def test_fallback_can_be_disabled(self, book_file, config):
        config.traders.fall_back_to_api = False
        book = TraderPriceBook.load(book_file, config=config)
        assert book.resolve(make_item("Salewa", 10_000, 19_000)) is None

    def test_normalise(self):
        assert normalise("  Bottle   Of Water ") == "bottle of water"


class TestMargins:
    def test_profit_margin_formula(self):
        assert profit_margin(10_000, 15_000) == 5_000
        assert profit_margin(15_000, 10_000) == -5_000

    def test_margin_result_fields(self, config):
        book = TraderPriceBook(config=config)
        m = book.compute_margin(make_item("Thing", 10_000, 15_000, width=2, height=1))
        assert m.profit_margin == 5_000
        assert m.margin_ratio == 0.5
        assert m.profit_per_slot == 2_500

    def test_no_margin_without_trader_price(self, config):
        from flea_bot.scraper.models import ItemPrice

        book = TraderPriceBook(config=config)
        assert book.compute_margin(ItemPrice("x", "X", price=1_000)) is None

    def test_compute_margins_filters_unsellable(self, config):
        from flea_bot.scraper.models import ItemPrice

        book = TraderPriceBook(config=config)
        results = book.compute_margins(
            [make_item("A", 1_000, 2_000), ItemPrice("b", "B", price=500)]
        )
        assert [r.item_name for r in results] == ["A"]


class TestRanking:
    def _margins(self, config, specs):
        book = TraderPriceBook(config=config)
        return book.compute_margins([make_item(*s) for s in specs])

    def test_sorts_by_margin_descending(self, config):
        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        margins = self._margins(
            config, [("A", 10_000, 12_000), ("B", 10_000, 30_000), ("C", 10_000, 20_000)]
        )
        result = rank_items(margins, config=config, sort_by=SortKey.MARGIN)
        assert [r.item_name for r in result.items] == ["B", "C", "A"]

    def test_respects_top_n(self, config):
        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        margins = self._margins(config, [(f"I{i}", 10_000, 20_000 + i) for i in range(10)])
        assert len(rank_items(margins, config=config, top_n=3).items) == 3

    def test_filters_below_min_margin(self, config):
        config.thresholds.min_margin = 5_000
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        result = rank_items(self._margins(config, [("A", 10_000, 11_000)]), config=config)
        assert result.items == []
        assert result.rejections[0].reason is RejectReason.BELOW_MIN_MARGIN

    def test_filters_below_min_ratio(self, config):
        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0.5
        config.thresholds.min_flea_price = 0
        result = rank_items(self._margins(config, [("A", 100_000, 110_000)]), config=config)
        assert result.rejections[0].reason is RejectReason.BELOW_MIN_RATIO

    def test_filters_price_bounds(self, config):
        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 5_000
        config.thresholds.max_flea_price = 50_000
        margins = self._margins(
            config, [("Cheap", 1_000, 9_000), ("Pricey", 100_000, 200_000)]
        )
        result = rank_items(margins, config=config)
        reasons = {r.reason for r in result.rejections}
        assert reasons == {RejectReason.BELOW_MIN_PRICE, RejectReason.ABOVE_MAX_PRICE}

    def test_rejects_negative_margin(self, config):
        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        result = rank_items(self._margins(config, [("A", 20_000, 10_000)]), config=config)
        assert result.rejections[0].reason is RejectReason.NEGATIVE_MARGIN

    def test_volatility_filter(self, config):
        from flea_bot.database.repository import PriceStats

        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        config.thresholds.max_volatility = 0.2

        margins = self._margins(config, [("A", 10_000, 20_000)])
        volatile = PriceStats("a", "A", samples=10, mean=10_000.0, minimum=5_000,
                              maximum=15_000, latest=10_000, stddev=5_000.0, window_hours=24)
        result = rank_items(margins, stats={"a": volatile}, config=config)
        assert result.rejections[0].reason is RejectReason.TOO_VOLATILE

    def test_volatility_ignored_when_samples_too_few(self, config):
        from flea_bot.database.repository import PriceStats

        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        config.thresholds.max_volatility = 0.2

        margins = self._margins(config, [("A", 10_000, 20_000)])
        # Same volatility, but only 2 samples — not enough to act on.
        noisy = PriceStats("a", "A", samples=2, mean=10_000.0, minimum=5_000,
                           maximum=15_000, latest=10_000, stddev=5_000.0, window_hours=24)
        result = rank_items(margins, stats={"a": noisy}, config=config)
        assert len(result.items) == 1

    def test_per_slot_sort_prefers_compact_items(self, config):
        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        book = TraderPriceBook(config=config)
        margins = book.compute_margins(
            [
                make_item("Big", 10_000, 22_000, width=4, height=2),   # 12k / 8 slots
                make_item("Small", 10_000, 18_000, width=1, height=1),  # 8k / 1 slot
            ]
        )
        result = rank_items(margins, config=config, sort_by=SortKey.PER_SLOT)
        assert result.items[0].item_name == "Small"

    def test_explicit_kwargs_override_config(self, config):
        config.thresholds.min_margin = 1_000_000
        margins = self._margins(config, [("A", 10_000, 20_000)])
        assert len(rank_items(margins, config=config, min_margin=0,
                              min_margin_ratio=0, min_price=0).items) == 1

    def test_none_disables_volatility_check(self, config):
        from flea_bot.database.repository import PriceStats

        config.thresholds.min_margin = 0
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        config.thresholds.max_volatility = 0.01
        margins = self._margins(config, [("A", 10_000, 20_000)])
        wild = PriceStats("a", "A", samples=10, mean=10_000.0, minimum=1,
                          maximum=99_999, latest=10_000, stddev=9_000.0, window_hours=24)
        result = rank_items(margins, stats={"a": wild}, config=config, max_volatility=None)
        assert len(result.items) == 1

    def test_rejection_summary_counts(self, config):
        config.thresholds.min_margin = 5_000
        config.thresholds.min_margin_ratio = 0
        config.thresholds.min_flea_price = 0
        margins = self._margins(
            config, [("A", 10_000, 11_000), ("B", 10_000, 11_500), ("C", 10_000, 20_000)]
        )
        result = rank_items(margins, config=config)
        assert result.rejection_summary()["below_min_margin"] == 2
        assert result.considered == 3
