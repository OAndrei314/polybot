"""Polymarket public API client.

Endpoints used:
  gamma-api.polymarket.com   - market/event metadata, resolution outcomes
  data-api.polymarket.com    - per-trade activity feed (with proxyWallet)

Both APIs are public and unauthenticated. We rate-limit ourselves at ~5
req/sec to be polite. All responses are cached to disk for resumability.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
USER_AGENT = "polybot-research/1.0 (research only, no trading)"

CACHE_ROOT = Path("data/polymarket_alpha/cache")

# Polite throttle: target ~5 req/sec across the pipeline.
MIN_INTERVAL_S = 0.20
_last_request_ts = 0.0


def _throttle() -> None:
    global _last_request_ts
    now = time.time()
    wait = MIN_INTERVAL_S - (now - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()


def _cache_key(url: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return CACHE_ROOT / h[:2] / f"{h}.json.gz"


def _read_cache(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as f:
        json.dump(data, f)


class HardClientError(Exception):
    """HTTP 4xx other than 429 - retrying won't help. Caller can stop pagination."""


def get(url: str, use_cache: bool = True, max_retries: int = 3) -> Any:
    """GET with retry, throttle, and gzip-on-disk cache.

    Raises HardClientError on 400/404 etc. so callers can stop paginating
    rather than retrying a request the server has rejected.
    """
    path = _cache_key(url)
    if use_cache:
        cached = _read_cache(path)
        if cached is not None:
            return cached

    last_err: Exception | None = None
    for attempt in range(max_retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            _write_cache(path, data)
            return data
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                time.sleep(2 ** attempt)
            elif 400 <= e.code < 500:
                raise HardClientError(f"{e.code} {e.reason}: {url}") from e
            else:
                time.sleep(0.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {max_retries} attempts: {url} :: {last_err}")


def gamma_markets(closed: bool | None = None, limit: int = 100,
                  offset: int = 0, order: str = "volumeNum",
                  ascending: bool = False) -> list[dict]:
    """Page of markets from Gamma API. Use offset to paginate.

    Sort key `volumeNum` returns the actual top-volume markets (Trump 2024
    at $1.5B, Kamala at $1B, etc.). `volume` is a string field that
    sorts alphabetically and returns useless $100-volume markets first.
    """
    params = [f"limit={limit}", f"offset={offset}"]
    if order: params.append(f"order={order}")
    params.append(f"ascending={'true' if ascending else 'false'}")
    if closed is not None: params.append(f"closed={'true' if closed else 'false'}")
    url = f"{GAMMA_BASE}/markets?" + "&".join(params)
    return get(url)


def data_trades_for_market(slug: str, limit: int = 500, offset: int = 0) -> list[dict]:
    """Trades on a specific market by slug. The data-api supports
    filtering by market via the `market` parameter."""
    url = f"{DATA_BASE}/trades?market={slug}&limit={limit}&offset={offset}"
    return get(url)


def data_trades_recent(limit: int = 500, offset: int = 0) -> list[dict]:
    """Recent trades across all markets (for initial discovery)."""
    url = f"{DATA_BASE}/trades?limit={limit}&offset={offset}"
    return get(url)
