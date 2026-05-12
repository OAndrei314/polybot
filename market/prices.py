from __future__ import annotations

import ast
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
import websocket

DEFAULT_EVENT_SLUGS = [
    "elon-musk-of-tweets-april-24-may-1",
    "elon-musk-of-tweets-april-28-may-5",
    "elon-musk-of-tweets-may-1-may-8",
    "elon-musk-of-tweets-april-30-may-2",
    "elon-musk-of-tweets-may-2-may-4",
    "elon-musk-of-tweets-may-2026",
]

# Backward compatibility for modules importing EVENT_SLUGS.
EVENT_SLUGS = list(DEFAULT_EVENT_SLUGS)

GAMMA_EVENT_URL = "https://gamma-api.polymarket.com/events?slug={slug}"
GAMMA_SERIES_URL = (
    "https://gamma-api.polymarket.com/events?series_id={series_id}&closed=false&limit=50"
)
# Polymarket series IDs for the Elon-tweet markets. These are the canonical
# series we track; new weekly/48h/monthly events are auto-listed under them
# the moment Polymarket creates them — much earlier than xtracker registers.
ELON_TWEET_SERIES_IDS = (10000, 10816, 10181)
CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={token_id}"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
XTRACKER_TRACKINGS_URL = (
    "https://xtracker.polymarket.com/api/users/elonmusk/trackings?activeOnly={active_only}"
)


@dataclass(frozen=True)
class Bracket:
    event_slug: str
    event_title: str
    market_slug: str
    question: str
    label: str
    low: int
    high: int
    condition_id: str
    yes_token_id: str
    no_token_id: str
    yes_mid: float
    no_mid: float


class PolymarketPrices:
    """Fast price reader for YES/NO buy+sell using CLOB websocket + REST fallback."""

    def __init__(
        self,
        event_slugs: list[str] | None = None,
        stale_after_s: float = 6.0,
        request_timeout_s: float = 4.0,
        event_refresh_s: float = 60.0,
        max_events: int = 12,
    ) -> None:
        initial_slugs = event_slugs or list(EVENT_SLUGS)
        self._manual_event_slugs = event_slugs is not None
        self.event_slugs = self._normalize_slugs(initial_slugs)
        self.stale_after_s = stale_after_s
        self.request_timeout_s = request_timeout_s
        env_refresh = os.getenv("POLYBOT_EVENT_REFRESH_S")
        env_max_events = os.getenv("POLYBOT_EVENT_MAX_EVENTS")
        self.event_refresh_s = max(
            20.0,
            float(env_refresh) if env_refresh not in (None, "") else float(event_refresh_s),
        )
        self.max_events = max(
            len(self.event_slugs),
            int(env_max_events) if env_max_events not in (None, "") else int(max_events),
        )

        self.session = requests.Session()
        self.brackets: list[Bracket] = []
        self._token_to_condition: dict[str, str] = {}
        self._levels: dict[str, dict[str, dict[float, float]]] = {}
        self._bbo: dict[str, dict[str, float | None]] = {}
        self._last_ws_event_unix: float | None = None
        self._ws_connected = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ws_thread: threading.Thread | None = None
        self._ws_app: websocket.WebSocketApp | None = None

        self._last_event_refresh_unix = 0.0
        self._event_payload_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _normalize_slugs(slugs: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for slug in slugs:
            s = str(slug or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out

    @staticmethod
    def _parse_iso(raw: Any) -> datetime | None:
        if raw in (None, ""):
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _slug_from_market_link(link: str) -> str:
        txt = str(link or "").strip()
        if not txt:
            return ""
        return txt.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _parse_token_ids(raw: Any) -> tuple[str, str]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = ast.literal_eval(raw)
        if not isinstance(raw, list):
            return "", ""
        yes = str(raw[0]) if len(raw) > 0 else ""
        no = str(raw[1]) if len(raw) > 1 else ""
        return yes, no

    @staticmethod
    def _parse_outcome_prices(raw: Any) -> tuple[float, float]:
        vals: list[Any]
        if isinstance(raw, str):
            try:
                vals = json.loads(raw)
            except Exception:
                vals = ast.literal_eval(raw)
        elif isinstance(raw, list):
            vals = raw
        else:
            vals = [0.5, 0.5]
        try:
            yes = float(vals[0])
        except Exception:
            yes = 0.5
        try:
            no = float(vals[1])
        except Exception:
            no = max(0.0, 1.0 - yes)
        return yes, no

    @staticmethod
    def _parse_bracket_bounds(question: str) -> tuple[int, int] | None:
        m = re.search(r"post\s+(\d+)-(\d+)\s+tweets", question, flags=re.IGNORECASE)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"post\s+(\d+)\+\s+tweets", question, flags=re.IGNORECASE)
        if m:
            low = int(m.group(1))
            return low, 9999
        return None

    @staticmethod
    def _label_from_bounds(low: int, high: int) -> str:
        return f"{low}+" if high >= 9999 else f"{low}-{high}"

    @staticmethod
    def _float_or_none(v: Any) -> float | None:
        try:
            return float(v)
        except Exception:
            return None

    def _event_payload(self, slug: str, force: bool = False) -> dict[str, Any] | None:
        now = time.time()
        cached = self._event_payload_cache.get(slug)
        if cached and not force and (now - cached[0]) <= min(90.0, self.event_refresh_s):
            return cached[1]

        try:
            resp = self.session.get(
                GAMMA_EVENT_URL.format(slug=slug), timeout=self.request_timeout_s
            )
            if not resp.ok:
                return None
            events = resp.json() or []
            if not events:
                return None
            event = events[0]
            if isinstance(event, dict):
                self._event_payload_cache[slug] = (now, event)
                return event
        except Exception:
            return None
        return None

    def _event_unresolved(self, slug: str, force: bool = False) -> bool:
        event = self._event_payload(slug, force=force)
        if not isinstance(event, dict):
            # If we cannot fetch metadata, keep the slug to avoid accidentally
            # dropping a tradable event.
            return True

        markets = event.get("markets") or []
        if not isinstance(markets, list) or not markets:
            return not bool(event.get("closed"))

        for market in markets:
            if not isinstance(market, dict):
                continue
            if bool(market.get("acceptingOrders")):
                return True
            if not bool(market.get("closed")):
                return True
            uma_status = str(market.get("umaResolutionStatus") or "").strip().lower()
            if uma_status and uma_status != "resolved":
                return True
        return not bool(event.get("closed"))

    def _discover_via_gamma(self) -> dict[str, dict[str, Any]]:
        """Pull Elon-tweet events directly from Polymarket Gamma series IDs.

        Returns a dict keyed by slug with each event's metadata. This is the
        primary discovery channel — new markets land here as soon as Polymarket
        creates them, which is hours earlier than xtracker registration.
        """
        out: dict[str, dict[str, Any]] = {}
        for series_id in ELON_TWEET_SERIES_IDS:
            try:
                resp = self.session.get(
                    GAMMA_SERIES_URL.format(series_id=series_id),
                    timeout=max(6.0, self.request_timeout_s),
                )
                if not resp.ok:
                    continue
                events = resp.json() or []
            except Exception:
                continue
            for evt in events:
                if not isinstance(evt, dict):
                    continue
                slug = str(evt.get("slug") or "")
                if not slug:
                    continue
                # Only Elon-Musk tweet brackets — the series can occasionally
                # contain other people's events.
                if "elon-musk" not in slug or "tweets" not in slug:
                    continue
                if evt.get("closed"):
                    continue
                out[slug] = evt
                # Warm the per-slug payload cache so downstream callers do not
                # re-fetch what we already have.
                self._event_payload_cache[slug] = (time.time(), evt)
        return out

    def _discover_event_slugs(self) -> list[str]:
        if self._manual_event_slugs:
            return list(self.event_slugs)

        now = datetime.now(timezone.utc)
        ranked: list[tuple[tuple[Any, ...], str]] = []
        seen: set[str] = set()

        # Primary: Gamma series queries (hits Polymarket directly, no
        # xtracker registration delay).
        gamma_events = self._discover_via_gamma()
        for slug, evt in gamma_events.items():
            seen.add(slug)
            end_dt = self._parse_iso(evt.get("endDate"))
            markets = evt.get("markets") or []
            open_count = sum(
                1 for m in markets if isinstance(m, dict) and m.get("acceptingOrders")
            )
            seconds_from_now = abs((end_dt - now).total_seconds()) if end_dt else float("inf")
            end_sort = -(end_dt.timestamp()) if end_dt else float("inf")
            ranked.append((
                (0 if open_count > 0 else 1, 0, seconds_from_now, end_sort),
                slug,
            ))

        # Fallback: xtracker enumeration for anything Gamma missed.
        try:
            resp = self.session.get(
                XTRACKER_TRACKINGS_URL.format(active_only="false"),
                timeout=max(6.0, self.request_timeout_s),
            )
            rows = (
                resp.json().get("data") if resp.ok and isinstance(resp.json(), dict) else None
            )
        except Exception:
            rows = None

        if isinstance(rows, list):
            for tr in rows:
                if not isinstance(tr, dict):
                    continue
                slug = self._slug_from_market_link(str(tr.get("marketLink") or ""))
                if not slug or slug in seen:
                    continue
                seen.add(slug)

                if not self._event_unresolved(slug):
                    continue

                end_dt = self._parse_iso(tr.get("endDate"))
                is_active = bool(tr.get("isActive"))
                event_payload = self._event_payload(slug) or {}
                markets = event_payload.get("markets") or []
                open_count = sum(
                    1 for m in markets if isinstance(m, dict) and m.get("acceptingOrders")
                )
                seconds_from_now = abs((end_dt - now).total_seconds()) if end_dt else float("inf")
                end_sort = -(end_dt.timestamp()) if end_dt else float("inf")
                ranked.append((
                    (0 if open_count > 0 else 1, 0 if is_active else 1, seconds_from_now, end_sort),
                    slug,
                ))

        ranked.sort(key=lambda pair: pair[0])
        discovered = [slug for _, slug in ranked][: self.max_events]

        if not discovered:
            return list(self.event_slugs)

        return self._normalize_slugs(discovered)

    def refresh_event_universe(self, force: bool = False) -> bool:
        if self._manual_event_slugs:
            return False

        now = time.time()
        if not force and (now - self._last_event_refresh_unix) < self.event_refresh_s:
            return False

        self._last_event_refresh_unix = now
        discovered = self._discover_event_slugs()
        if not discovered:
            return False

        discovered_set = set(discovered)
        current_set = set(self.event_slugs)
        if discovered_set == current_set:
            return False

        retained = [slug for slug in self.event_slugs if slug in discovered_set]
        added = [slug for slug in discovered if slug not in current_set]
        self.event_slugs = retained + added
        self.load_brackets()
        self.refresh_stale_tokens(force_all=True)
        self._restart_ws_if_running()
        return True

    def load_brackets(self) -> list[Bracket]:
        loaded: list[Bracket] = []
        seen_conditions: set[str] = set()

        for slug in self.event_slugs:
            try:
                resp = self.session.get(
                    GAMMA_EVENT_URL.format(slug=slug), timeout=self.request_timeout_s
                )
                if not resp.ok:
                    continue
                events = resp.json() or []
            except Exception:
                continue
            if not events:
                continue

            event = events[0]
            event_title = str(event.get("title") or slug)

            for market in event.get("markets") or []:
                question = str(market.get("question") or "")
                bounds = self._parse_bracket_bounds(question)
                if bounds is None:
                    continue
                low, high = bounds

                yes_token, no_token = self._parse_token_ids(market.get("clobTokenIds") or [])
                if not yes_token or not no_token:
                    continue

                yes_mid, no_mid = self._parse_outcome_prices(market.get("outcomePrices"))

                condition_id = str(market.get("conditionId") or "")
                if not condition_id or condition_id in seen_conditions:
                    continue

                seen_conditions.add(condition_id)
                loaded.append(
                    Bracket(
                        event_slug=slug,
                        event_title=event_title,
                        market_slug=str(market.get("slug") or ""),
                        question=question,
                        label=self._label_from_bounds(low, high),
                        low=low,
                        high=high,
                        condition_id=condition_id,
                        yes_token_id=yes_token,
                        no_token_id=no_token,
                        yes_mid=yes_mid,
                        no_mid=no_mid,
                    )
                )

        loaded.sort(key=lambda b: (b.event_slug, b.low, b.high))

        with self._lock:
            self.brackets = loaded
            self._token_to_condition.clear()
            keep_tokens = {b.yes_token_id for b in loaded} | {b.no_token_id for b in loaded}
            self._levels = {
                token: levels
                for token, levels in self._levels.items()
                if token in keep_tokens
            }
            self._bbo = {
                token: bbo
                for token, bbo in self._bbo.items()
                if token in keep_tokens
            }
            for b in loaded:
                self._token_to_condition[b.yes_token_id] = b.condition_id
                self._token_to_condition[b.no_token_id] = b.condition_id

        return loaded

    def _all_tokens(self) -> list[str]:
        out: list[str] = []
        for b in self.brackets:
            out.append(b.yes_token_id)
            out.append(b.no_token_id)
        return out

    def _set_token_book(self, token: str, bids: list[dict], asks: list[dict]) -> None:
        bid_map: dict[float, float] = {}
        ask_map: dict[float, float] = {}
        for row in bids:
            p = self._float_or_none(row.get("price"))
            s = self._float_or_none(row.get("size"))
            if p is not None and s is not None and s > 0:
                bid_map[p] = s
        for row in asks:
            p = self._float_or_none(row.get("price"))
            s = self._float_or_none(row.get("size"))
            if p is not None and s is not None and s > 0:
                ask_map[p] = s

        bid = max(bid_map.keys()) if bid_map else None
        ask = min(ask_map.keys()) if ask_map else None

        self._levels[token] = {"bids": bid_map, "asks": ask_map}
        self._bbo[token] = {"bid": bid, "ask": ask, "ts": time.time()}

    def _apply_price_changes(self, token: str, changes: list[dict]) -> None:
        levels = self._levels.setdefault(token, {"bids": {}, "asks": {}})
        for change in changes:
            side = str(change.get("side") or "").upper()
            price = self._float_or_none(change.get("price"))
            size = self._float_or_none(change.get("size"))
            if price is None:
                continue
            if side == "BUY":
                level = levels["bids"]
            elif side == "SELL":
                level = levels["asks"]
            else:
                continue

            if size is None or size <= 0:
                level.pop(price, None)
            else:
                level[price] = size

        bid = max(levels["bids"].keys()) if levels["bids"] else None
        ask = min(levels["asks"].keys()) if levels["asks"] else None
        self._bbo[token] = {"bid": bid, "ask": ask, "ts": time.time()}

    def refresh_stale_tokens(self, force_all: bool = False) -> None:
        now = time.time()
        tokens = self._all_tokens()
        if not tokens:
            return

        with self._lock:
            if force_all:
                stale = tokens
            else:
                stale = [
                    t
                    for t in tokens
                    if t not in self._bbo
                    or (now - float(self._bbo[t].get("ts") or 0.0)) > self.stale_after_s
                ]

        if not stale:
            return

        def fetch_one(token: str) -> tuple[str, list[dict], list[dict]] | None:
            try:
                r = self.session.get(
                    CLOB_BOOK_URL.format(token_id=token), timeout=self.request_timeout_s
                )
                if not r.ok:
                    return None
                d = r.json()
                bids = d.get("bids") or []
                asks = d.get("asks") or []
                return token, bids, asks
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=24) as pool:
            futures = [pool.submit(fetch_one, token) for token in stale]
            for fut in as_completed(futures):
                row = fut.result()
                if row is None:
                    continue
                token, bids, asks = row
                with self._lock:
                    self._set_token_book(token, bids, asks)

    def _on_ws_open(self, ws: websocket.WebSocketApp) -> None:
        tokens = self._all_tokens()
        if not tokens:
            self._ws_connected = False
            return
        payload = {
            "type": "market",
            "assets_ids": tokens,
            "custom_feature_enabled": True,
        }
        ws.send(json.dumps(payload))
        self._ws_connected = True

    def _on_ws_message(self, _ws: websocket.WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except Exception:
            return

        events = data if isinstance(data, list) else [data]
        if not events:
            return

        with self._lock:
            self._last_ws_event_unix = time.time()
            for event in events:
                token = str(event.get("asset_id") or event.get("market") or "")
                if not token or token not in self._token_to_condition:
                    continue

                event_type = str(event.get("event_type") or event.get("type") or "")
                if event_type in ("book", "orderbook"):
                    self._set_token_book(token, event.get("bids") or [], event.get("asks") or [])
                elif event_type == "price_change":
                    self._apply_price_changes(token, event.get("changes") or [])
                elif event_type == "best_bid_ask":
                    bid = self._float_or_none(event.get("best_bid"))
                    ask = self._float_or_none(event.get("best_ask"))
                    if bid is not None or ask is not None:
                        prev = self._bbo.get(token) or {}
                        self._bbo[token] = {
                            "bid": bid if bid is not None else prev.get("bid"),
                            "ask": ask if ask is not None else prev.get("ask"),
                            "ts": time.time(),
                        }

    def _on_ws_close(self, _ws: websocket.WebSocketApp, _code: int, _msg: str) -> None:
        self._ws_connected = False

    def _on_ws_error(self, _ws: websocket.WebSocketApp, _error: Any) -> None:
        self._ws_connected = False

    def _ws_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_close=self._on_ws_close,
                    on_error=self._on_ws_error,
                )
                self._ws_app = ws
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception:
                self._ws_connected = False
            finally:
                self._ws_app = None
            if not self._stop.is_set():
                time.sleep(2.0)

    def _restart_ws_if_running(self) -> None:
        ws = self._ws_app
        if ws is None:
            return
        try:
            ws.close()
        except Exception:
            pass

    def start(self) -> None:
        if not self.brackets:
            self.refresh_event_universe(force=True)
            self.load_brackets()
        self.refresh_stale_tokens(force_all=True)
        if self._ws_thread is None:
            self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
            self._ws_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._restart_ws_if_running()

    def ws_ok(self, max_silence_s: float = 10.0) -> bool:
        if not self._ws_connected:
            return False
        if self._last_ws_event_unix is None:
            return False
        return (time.time() - self._last_ws_event_unix) <= max_silence_s

    def feed_state(self, max_silence_s: float = 10.0) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            tokens = self._all_tokens()
            bbo_count = len(self._bbo)
            stale_count = 0
            empty_count = 0
            max_age = None
            for token in tokens:
                bbo = self._bbo.get(token) or {}
                age = None
                if bbo.get("ts"):
                    age = now - float(bbo.get("ts") or 0.0)
                    max_age = age if max_age is None else max(max_age, age)
                    if age > self.stale_after_s:
                        stale_count += 1
                if bbo.get("bid") is None or bbo.get("ask") is None:
                    empty_count += 1
        last_ws_age = None
        if self._last_ws_event_unix is not None:
            last_ws_age = round(now - self._last_ws_event_unix, 3)
        return {
            "ws_connected": bool(self._ws_connected),
            "ws_ok": self.ws_ok(max_silence_s=max_silence_s),
            "last_ws_event_age_s": last_ws_age,
            "tracked_tokens": len(tokens),
            "bbo_tokens": bbo_count,
            "stale_tokens": stale_count,
            "empty_tokens": empty_count,
            "max_bbo_age_s": round(max_age, 3) if max_age is not None else None,
            "stale_after_s": self.stale_after_s,
        }

    def snapshot(self, allow_synthetic_fallback: bool = False) -> list[dict[str, Any]]:
        now = time.time()
        rows: list[dict[str, Any]] = []

        with self._lock:
            for b in self.brackets:
                yes = self._bbo.get(b.yes_token_id, {})
                no = self._bbo.get(b.no_token_id, {})
                yes_bid = yes.get("bid")
                yes_ask = yes.get("ask")
                no_bid = no.get("bid")
                no_ask = no.get("ask")

                yes_real = yes_bid is not None and yes_ask is not None
                no_real = no_bid is not None and no_ask is not None
                if not allow_synthetic_fallback and (not yes_real or not no_real):
                    continue

                # Optional display-only fallback. Live/paper trading should keep
                # allow_synthetic_fallback=False so empty books are not tradable.
                if not yes_real:
                    width = 0.003 if 0.1 <= b.yes_mid <= 0.9 else 0.001
                    yes_bid = max(0.001, round(b.yes_mid - width, 6))
                    yes_ask = min(0.999, round(b.yes_mid + width, 6))
                if not no_real:
                    width = 0.003 if 0.1 <= b.no_mid <= 0.9 else 0.001
                    no_bid = max(0.001, round(b.no_mid - width, 6))
                    no_ask = min(0.999, round(b.no_mid + width, 6))

                yes_age = None
                no_age = None
                if yes.get("ts"):
                    yes_age = round(now - float(yes.get("ts") or 0.0), 3)
                if no.get("ts"):
                    no_age = round(now - float(no.get("ts") or 0.0), 3)
                rows.append(
                    {
                        "event_slug": b.event_slug,
                        "event_title": b.event_title,
                        "condition_id": b.condition_id,
                        "label": b.label,
                        "question": b.question,
                        "yes_buy": yes_ask,
                        "yes_sell": yes_bid,
                        "no_buy": no_ask,
                        "no_sell": no_bid,
                        "yes_age_s": yes_age,
                        "no_age_s": no_age,
                        "price_source": "clob_bbo"
                        if yes_real and no_real
                        else "gamma_mid_fallback",
                        "is_synthetic_price": not (yes_real and no_real),
                    }
                )

        rows.sort(key=lambda r: (str(r["event_slug"]), str(r["label"])))
        return rows


if __name__ == "__main__":
    feed = PolymarketPrices()
    feed.start()
    print(f"Loaded {len(feed.brackets)} brackets across {len(feed.event_slugs)} events")
    while True:
        time.sleep(2)
        feed.refresh_event_universe()
        feed.refresh_stale_tokens()
        snap = feed.snapshot()
        if snap:
            print(snap[0])
