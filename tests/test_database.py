"""Database tests — inserts, rolling averages, volatility."""

from __future__ import annotations

import math
from datetime import timedelta

from flea_bot.database.models import utcnow
from tests.conftest import make_item


class TestInserts:
    def test_insert_creates_item_and_snapshot(self, repo):
        repo.insert_snapshot(make_item("Bolts", 1_000, 2_000))
        assert repo.item_count() == 1
        assert repo.snapshot_count() == 1

    def test_repeated_inserts_reuse_one_item_row(self, repo):
        for price in (1_000, 1_100, 1_200):
            repo.insert_snapshot(
                make_item("Bolts", price, 2_000), recorded_at=utcnow() + timedelta(seconds=price)
            )
        assert repo.item_count() == 1
        assert repo.snapshot_count() == 3

    def test_bulk_insert_shares_one_timestamp(self, repo):
        repo.insert_snapshots([make_item(f"Item{i}", 1_000 + i) for i in range(5)])
        assert repo.snapshot_count() == 5
        stamps = {s.recorded_at for s in repo.history("item0")}
        assert len(stamps) == 1

    def test_snapshot_records_trader_price(self, repo):
        repo.insert_snapshot(make_item("Salewa", 20_000, 35_000, trader="Therapist"))
        snap = repo.latest_snapshot("salewa")
        assert snap.trader_price == 35_000
        assert snap.trader_name == "Therapist"

    def test_item_metadata_updates_on_reinsert(self, repo):
        repo.insert_snapshot(make_item("Thing", 1_000, width=1, height=1))
        repo.insert_snapshot(
            make_item("Thing", 1_000, width=2, height=3),
            recorded_at=utcnow() + timedelta(seconds=1),
        )
        with repo.session() as sess:
            from flea_bot.database.models import Item

            item = sess.get(Item, "thing")
            assert item.slots == 6


class TestStats:
    def _seed(self, repo, prices, name="Bolts"):
        base = utcnow() - timedelta(hours=1)
        for i, price in enumerate(prices):
            repo.insert_snapshot(
                make_item(name, price), recorded_at=base + timedelta(minutes=i)
            )

    def test_mean_and_range(self, repo):
        self._seed(repo, [1_000, 2_000, 3_000])
        s = repo.stats("bolts")
        assert s.samples == 3
        assert s.mean == 2_000
        assert (s.minimum, s.maximum) == (1_000, 3_000)
        assert s.latest == 3_000

    def test_stddev_matches_population_formula(self, repo):
        prices = [1_000, 2_000, 3_000, 4_000]
        self._seed(repo, prices)
        s = repo.stats("bolts")
        mean = sum(prices) / len(prices)
        expected = math.sqrt(sum((p - mean) ** 2 for p in prices) / len(prices))
        assert s.stddev == round(expected, 6) or abs(s.stddev - expected) < 1e-6

    def test_zero_volatility_for_flat_prices(self, repo):
        self._seed(repo, [5_000] * 5)
        s = repo.stats("bolts")
        # Float error must not produce a negative variance / NaN stddev.
        assert s.stddev == 0.0
        assert s.volatility == 0.0

    def test_volatility_is_scale_free(self, repo):
        self._seed(repo, [100, 200, 300], name="Cheap")
        self._seed(repo, [100_000, 200_000, 300_000], name="Expensive")
        cheap = repo.stats("cheap")
        pricey = repo.stats("expensive")
        assert abs(cheap.volatility - pricey.volatility) < 1e-9

    def test_window_excludes_old_snapshots(self, repo):
        old = utcnow() - timedelta(hours=48)
        repo.insert_snapshot(make_item("Bolts", 99_999), recorded_at=old)
        repo.insert_snapshot(make_item("Bolts", 1_000), recorded_at=utcnow())
        s = repo.stats("bolts", window_hours=24)
        assert s.samples == 1
        assert s.mean == 1_000

    def test_returns_none_without_data(self, repo):
        assert repo.stats("does-not-exist") is None

    def test_is_reliable_needs_three_samples(self, repo):
        self._seed(repo, [1_000, 2_000])
        assert repo.stats("bolts").is_reliable is False
        self._seed(repo, [3_000])
        assert repo.stats("bolts").is_reliable is True

    def test_stats_for_all_covers_every_item(self, repo):
        for i in range(3):
            self._seed(repo, [1_000 + i, 2_000 + i], name=f"Item{i}")
        allstats = repo.stats_for_all()
        assert set(allstats) == {"item0", "item1", "item2"}
        assert all(s.samples == 2 for s in allstats.values())


class TestPruning:
    def test_prune_removes_only_old_rows(self, repo):
        repo.insert_snapshot(make_item("Bolts", 1_000), recorded_at=utcnow() - timedelta(days=40))
        repo.insert_snapshot(make_item("Bolts", 1_100), recorded_at=utcnow())
        assert repo.prune_older_than(30) == 1
        assert repo.snapshot_count() == 1
