"""Price source: the tarkov.dev public GraphQL API.

Chosen over HTML scraping because it is a stable, documented, community-run
endpoint — no DOM breakage, no pagination guesswork, no robots.txt grey area.
It has no API key and no published hard rate limit, so we self-limit politely
(``prices.min_request_interval``) rather than hammering a free service.

Important caveat for SPT users: this serves LIVE Tarkov market data. SPT seeds
its flea from live prices but drifts between server versions and is affected by
your own server config. Treat these numbers as a reference baseline and let the
SQLite history (see :mod:`flea_bot.database`) tell you what your own server
actually does.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator

import httpx

from flea_bot.config import Config, get_config
from flea_bot.logging_setup import get_logger
from flea_bot.scraper.models import ItemPrice, VendorPrice

log = get_logger("scraper")

# `items` supports limit/offset; we page until a short page comes back.
ITEMS_QUERY = """
query Items($limit: Int!, $offset: Int!) {
  items(limit: $limit, offset: $offset) {
    id
    name
    shortName
    basePrice
    avg24hPrice
    low24hPrice
    high24hPrice
    lastLowPrice
    changeLast48hPercent
    width
    height
    types
    sellFor {
      priceRUB
      currency
      vendor { name }
    }
  }
}
"""


class PriceSourceError(RuntimeError):
    """Raised when the price source cannot be read after all retries."""


class RateLimiter:
    """Minimum-interval limiter. Not thread-safe; one client, one thread."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._last: float | None = None

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


class TarkovPriceClient:
    """Fetches item prices, with retries, backoff and rate limiting.

    Use as a context manager so the HTTP connection pool is closed::

        with TarkovPriceClient() as client:
            items = client.fetch_all()
    """

    name = "tarkov.dev"

    def __init__(
        self,
        config: Config | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config or get_config()
        self._cfg = self.config.prices
        self._limiter = RateLimiter(self._cfg.min_request_interval)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=self._cfg.request_timeout,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "flea-bot/0.1 (SPT offline tooling)",
            },
        )

    # ------------------------------------------------------------------
    def __enter__(self) -> TarkovPriceClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------
    def _post(self, query: str, variables: dict[str, object]) -> dict:
        """POST a GraphQL query, retrying transient failures with backoff."""
        last_error: Exception | None = None

        for attempt in range(self._cfg.max_retries + 1):
            self._limiter.wait()
            try:
                response = self._client.post(
                    self._cfg.api_url,
                    json={"query": query, "variables": variables},
                )
            except httpx.RequestError as exc:
                last_error = exc
                log.warning(
                    "Request error on attempt {}/{}: {}",
                    attempt + 1,
                    self._cfg.max_retries + 1,
                    exc,
                )
            else:
                # 429 / 5xx are worth retrying; other 4xx are our fault.
                if _is_retryable(response):
                    last_error = PriceSourceError(
                        f"HTTP {response.status_code} from price API: "
                        f"{response.text[:200]}"
                    )
                    retry_after = _parse_retry_after(response)
                    log.warning(
                        "HTTP {} on attempt {}/{}{}",
                        response.status_code,
                        attempt + 1,
                        self._cfg.max_retries + 1,
                        f", Retry-After={retry_after}s" if retry_after else "",
                    )
                    if retry_after:
                        time.sleep(retry_after)
                        continue
                elif response.is_error:
                    raise PriceSourceError(
                        f"HTTP {response.status_code} from price API: "
                        f"{response.text[:200]}"
                    )
                else:
                    payload = response.json()
                    if errors := payload.get("errors"):
                        # GraphQL errors are deterministic — retrying won't help.
                        raise PriceSourceError(f"GraphQL error: {errors}")
                    data = payload.get("data")
                    if data is None:
                        raise PriceSourceError("GraphQL response had no 'data' field")
                    return data

            if attempt < self._cfg.max_retries:
                delay = self._cfg.backoff_factor * (2**attempt)
                delay += random.uniform(0, delay * 0.25)  # jitter, avoid lockstep
                log.debug("Backing off {:.2f}s before retry", delay)
                time.sleep(delay)

        raise PriceSourceError(
            f"Price API failed after {self._cfg.max_retries + 1} attempts"
        ) from last_error

    # ------------------------------------------------------------------
    def iter_items(self) -> Iterator[ItemPrice]:
        """Yield every item, paging through the API.

        Stops on a short page, on an empty page, or at ``prices.max_pages``.
        """
        limit = self._cfg.page_size
        offset = 0
        page = 0
        seen: set[str] = set()

        while True:
            if self._cfg.max_pages is not None and page >= self._cfg.max_pages:
                log.warning(
                    "Hit max_pages={} — results are truncated. Raise it to fetch more.",
                    self._cfg.max_pages,
                )
                return

            log.debug("Fetching page {} (offset={}, limit={})", page + 1, offset, limit)
            data = self._post(ITEMS_QUERY, {"limit": limit, "offset": offset})
            raw_items = data.get("items") or []

            if not raw_items:
                log.debug("Empty page at offset {} — done.", offset)
                return

            for raw in raw_items:
                item = _parse_item(raw)
                if item is None:
                    continue
                # The API can overlap pages; don't double-count.
                if item.item_id in seen:
                    continue
                seen.add(item.item_id)
                yield item

            page += 1
            offset += limit

            if len(raw_items) < limit:
                log.debug("Short page ({} < {}) — done.", len(raw_items), limit)
                return

    def fetch_all(self) -> list[ItemPrice]:
        """Fetch every item as a list. See :meth:`iter_items` for streaming."""
        items = list(self.iter_items())
        log.info("Fetched {} items from {}", len(items), self._cfg.api_url)
        return items

    def fetch_as_dicts(self) -> list[dict[str, object]]:
        """The plain ``{item_name, price, quantity}`` output shape."""
        return [item.as_dict() for item in self.fetch_all()]


def _is_retryable(response: httpx.Response) -> bool:
    """Should this failed response be retried?

    The obvious cases are 429 and 5xx. The non-obvious one: when tarkov.dev's
    GraphQL backend is down, its edge returns **HTTP 422** with the body
    ``{"errors":["GraphQL server unavailable. Try again later."]}``. A 422
    normally means "your query is malformed, retrying is pointless", so
    without this check a transient outage looks like a permanent failure and
    aborts the whole fetch on the first attempt.
    """
    if response.status_code == 429 or response.status_code >= 500:
        return True
    return response.status_code == 422 and "unavailable" in response.text.lower()


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None  # HTTP-date form; not worth parsing, fall back to backoff


def _parse_item(raw: dict) -> ItemPrice | None:
    """Convert one GraphQL item node into an :class:`ItemPrice`.

    Returns None for items with no usable flea price (quest items, items banned
    from the flea, or anything the API has never seen listed).
    """
    item_id = raw.get("id")
    name = raw.get("name")
    if not item_id or not name:
        return None

    low = raw.get("lastLowPrice") or raw.get("low24hPrice")
    avg = raw.get("avg24hPrice")
    price = low or avg or 0
    if not price or price <= 0:
        return None

    sell_for = tuple(
        VendorPrice(
            vendor=(entry.get("vendor") or {}).get("name", "?"),
            price_rub=int(entry.get("priceRUB") or 0),
            currency=entry.get("currency") or "RUB",
        )
        for entry in (raw.get("sellFor") or [])
    )

    return ItemPrice(
        item_id=item_id,
        item_name=name,
        short_name=raw.get("shortName") or "",
        price=int(price),
        quantity=1,
        avg_24h=_opt_int(avg),
        low_24h=_opt_int(raw.get("low24hPrice")),
        high_24h=_opt_int(raw.get("high24hPrice")),
        base_price=_opt_int(raw.get("basePrice")),
        change_48h_percent=_opt_float(raw.get("changeLast48hPercent")),
        width=int(raw.get("width") or 1),
        height=int(raw.get("height") or 1),
        types=tuple(raw.get("types") or ()),
        sell_for=sell_for,
    )


def _opt_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and value else None


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
