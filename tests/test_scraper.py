"""Scraper tests — retries, pagination, parsing, rate limiting.

All HTTP is mocked; nothing here hits tarkov.dev.
"""

from __future__ import annotations

import time

import httpx
import pytest

from flea_bot.scraper.client import (
    PriceSourceError,
    RateLimiter,
    TarkovPriceClient,
    _parse_item,
)


def _node(item_id: str, name: str, price: int = 10_000, trader_price: int = 15_000):
    return {
        "id": item_id,
        "name": name,
        "shortName": name[:5],
        "basePrice": price // 2,
        "avg24hPrice": price,
        "low24hPrice": price - 500,
        "high24hPrice": price + 500,
        "lastLowPrice": price,
        "changeLast48hPercent": 1.5,
        "width": 1,
        "height": 1,
        "types": ["barter"],
        "sellFor": [
            {"priceRUB": price, "currency": "RUB", "vendor": {"name": "Flea Market"}},
            {"priceRUB": trader_price, "currency": "RUB", "vendor": {"name": "Therapist"}},
        ],
    }


def _transport(pages: list[list[dict]]):
    """Mock transport serving `pages` in order, then empty pages."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = calls["n"]
        calls["n"] += 1
        items = pages[i] if i < len(pages) else []
        return httpx.Response(200, json={"data": {"items": items}})

    return httpx.MockTransport(handler), calls


class TestParsing:
    def test_parses_full_node(self):
        item = _parse_item(_node("abc", "Bottle of water", 12_000, 18_000))
        assert item is not None
        assert item.item_name == "Bottle of water"
        assert item.price == 12_000
        assert item.quantity == 1
        best = item.best_trader()
        assert best is not None
        assert best.vendor == "Therapist"
        assert best.price_rub == 18_000

    def test_flea_vendor_excluded_from_best_trader(self):
        item = _parse_item(_node("abc", "Thing", 50_000, 10_000))
        assert item is not None
        # Flea pays more, but it is not a trader.
        assert item.best_trader().vendor == "Therapist"

    def test_skips_item_without_price(self):
        node = _node("abc", "Quest thing")
        node["lastLowPrice"] = None
        node["low24hPrice"] = None
        node["avg24hPrice"] = None
        assert _parse_item(node) is None

    def test_skips_item_without_id_or_name(self):
        assert _parse_item({"name": "no id"}) is None
        assert _parse_item({"id": "no-name"}) is None

    def test_falls_back_to_avg_when_no_low(self):
        node = _node("abc", "Thing", 10_000)
        node["lastLowPrice"] = None
        node["low24hPrice"] = None
        node["avg24hPrice"] = 9_000
        item = _parse_item(node)
        assert item is not None and item.price == 9_000

    def test_as_dict_shape(self):
        item = _parse_item(_node("abc", "Bolts", 3_000))
        d = item.as_dict()
        assert {"item_name", "price", "quantity"} <= set(d)
        assert d["item_name"] == "Bolts"
        assert d["price"] == 3_000

    def test_slots_from_dimensions(self):
        node = _node("abc", "Big thing")
        node["width"], node["height"] = 2, 3
        assert _parse_item(node).slots == 6


class TestPagination:
    def test_pages_until_short_page(self, config):
        config.prices.page_size = 2
        config.prices.min_request_interval = 0
        transport, calls = _transport(
            [
                [_node("a", "A"), _node("b", "B")],
                [_node("c", "C")],  # short page -> stop
            ]
        )
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            items = c.fetch_all()

        assert [i.item_name for i in items] == ["A", "B", "C"]
        assert calls["n"] == 2

    def test_stops_on_empty_page(self, config):
        config.prices.page_size = 2
        config.prices.min_request_interval = 0
        transport, calls = _transport([[_node("a", "A"), _node("b", "B")], []])
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            items = c.fetch_all()
        assert len(items) == 2
        assert calls["n"] == 2

    def test_respects_max_pages(self, config):
        config.prices.page_size = 1
        config.prices.max_pages = 2
        config.prices.min_request_interval = 0
        transport, calls = _transport([[_node(str(i), f"Item{i}")] for i in range(10)])
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            items = c.fetch_all()
        assert len(items) == 2
        assert calls["n"] == 2

    def test_deduplicates_overlapping_pages(self, config):
        config.prices.page_size = 2
        config.prices.min_request_interval = 0
        transport, _ = _transport(
            [
                [_node("a", "A"), _node("b", "B")],
                [_node("b", "B"), _node("c", "C")],  # 'b' repeats
                [],
            ]
        )
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            items = c.fetch_all()
        assert [i.item_name for i in items] == ["A", "B", "C"]


class TestRetries:
    def test_retries_on_500_then_succeeds(self, config):
        config.prices.min_request_interval = 0
        config.prices.backoff_factor = 0.0
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"data": {"items": [_node("a", "A")]}})

        transport = httpx.MockTransport(handler)
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            items = c.fetch_all()

        assert attempts["n"] == 3
        assert len(items) == 1

    def test_gives_up_after_max_retries(self, config):
        config.prices.min_request_interval = 0
        config.prices.backoff_factor = 0.0
        config.prices.max_retries = 2

        transport = httpx.MockTransport(lambda r: httpx.Response(503))
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            with pytest.raises(PriceSourceError, match="failed after 3 attempts"):
                c.fetch_all()

    def test_retries_on_network_error(self, config):
        config.prices.min_request_interval = 0
        config.prices.backoff_factor = 0.0
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"data": {"items": []}})

        transport = httpx.MockTransport(handler)
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            c.fetch_all()
        assert attempts["n"] == 2

    def test_retries_tarkov_dev_422_unavailable(self, config):
        """tarkov.dev signals backend outages with 422, not 5xx.

        Observed live: HTTP 422 with
        {"errors":["GraphQL server unavailable. Try again later."]}.
        Treating that as a permanent 4xx aborts the fetch on attempt one.
        """
        config.prices.min_request_interval = 0
        config.prices.backoff_factor = 0.0
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(
                    422, json={"errors": ["GraphQL server unavailable. Try again later."]}
                )
            return httpx.Response(200, json={"data": {"items": [_node("a", "A")]}})

        transport = httpx.MockTransport(handler)
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            items = c.fetch_all()

        assert attempts["n"] == 3
        assert len(items) == 1

    def test_422_from_a_bad_query_is_not_retried(self, config):
        """A genuine 422 (malformed query) must still fail fast."""
        config.prices.min_request_interval = 0
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            return httpx.Response(422, json={"errors": ["Cannot query field 'nope'"]})

        transport = httpx.MockTransport(handler)
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            with pytest.raises(PriceSourceError, match="HTTP 422"):
                c.fetch_all()
        assert attempts["n"] == 1

    def test_retryable_error_message_includes_body(self, config):
        """The final error must say *why*, not just the status code."""
        config.prices.min_request_interval = 0
        config.prices.backoff_factor = 0.0
        config.prices.max_retries = 1

        transport = httpx.MockTransport(
            lambda r: httpx.Response(
                422, json={"errors": ["GraphQL server unavailable. Try again later."]}
            )
        )
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            with pytest.raises(PriceSourceError) as exc:
                c.fetch_all()
        assert "failed after 2 attempts" in str(exc.value)
        assert "unavailable" in str(exc.value.__cause__).lower()

    def test_does_not_retry_client_error(self, config):
        config.prices.min_request_interval = 0
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            return httpx.Response(400, text="bad query")

        transport = httpx.MockTransport(handler)
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            with pytest.raises(PriceSourceError, match="HTTP 400"):
                c.fetch_all()
        assert attempts["n"] == 1, "4xx is our bug; retrying just wastes time"

    def test_graphql_errors_raise(self, config):
        config.prices.min_request_interval = 0
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"errors": [{"message": "bad field"}]})
        )
        with TarkovPriceClient(config, client=httpx.Client(transport=transport)) as c:
            with pytest.raises(PriceSourceError, match="GraphQL error"):
                c.fetch_all()


class TestRateLimiter:
    def test_enforces_minimum_interval(self):
        limiter = RateLimiter(0.05)
        start = time.monotonic()
        for _ in range(3):
            limiter.wait()
        # First call is free; the next two each wait ~50ms.
        assert time.monotonic() - start >= 0.09

    def test_zero_interval_does_not_sleep(self):
        limiter = RateLimiter(0)
        start = time.monotonic()
        for _ in range(50):
            limiter.wait()
        assert time.monotonic() - start < 0.05
