from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from model.predictor import PredictionModel
from market.prices import PolymarketPrices
from signals.stream import TweetStream
from signals.health import XTrackerHealth


def _fmt_price(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{float(v):.3f}"
    return "n/a"


def _fmt_ts(raw: str | None) -> str:
    if not raw:
        return "n/a"
    return raw.replace("T", " ").replace("+00:00", "Z")


def _tail_lines_bytes(path: str, keep_lines: int, chunk_size: int = 65536) -> bytes:
    if keep_lines <= 0 or not os.path.exists(path):
        return b""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        if pos <= 0:
            return b""
        buf = b""
        line_count = 0
        while pos > 0 and line_count <= keep_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos, os.SEEK_SET)
            buf = f.read(read_size) + buf
            line_count = buf.count(b"\n")
    lines = buf.splitlines()[-keep_lines:]
    if not lines:
        return b""
    return b"\n".join(lines) + b"\n"


class TerminalLogger:
    def __init__(
        self,
        tick_s: float = 3.0,
        out_log: str = "lite_terminal_events.jsonl",
        log_max_lines: int = 400,
        log_max_bytes: int = 500_000,
    ) -> None:
        self.tick_s = tick_s
        self.out_log = out_log
        self.log_max_lines = max(1, int(log_max_lines))
        self.log_max_bytes = max(10_000, int(log_max_bytes))
        self._last_trim_unix = 0.0
        self.prices = PolymarketPrices()
        self.tweets = TweetStream()
        self.health = XTrackerHealth()
        self.model = PredictionModel()

    def _append_log(self, row: dict[str, Any]) -> None:
        with open(self.out_log, "a") as f:
            f.write(json.dumps(row) + "\n")
        self._maybe_trim_log()

    def _maybe_trim_log(self) -> None:
        try:
            size = os.path.getsize(self.out_log)
        except OSError:
            return
        if size <= self.log_max_bytes:
            return
        now = time.time()
        if (now - self._last_trim_unix) < 10.0:
            return
        data = _tail_lines_bytes(self.out_log, self.log_max_lines)
        with open(self.out_log, "wb") as f:
            f.write(data)
        self._last_trim_unix = now

    def run(self) -> None:
        self.prices.start()
        self.tweets.start()

        while True:
            t0 = time.time()
            try:
                self.prices.refresh_event_universe()
                self.health.set_event_slugs(list(self.prices.event_slugs))
                self.prices.refresh_stale_tokens()
                price_rows = self.prices.snapshot()
                tweet_state = self.tweets.poll()
                health_state = self.health.poll(tweet_state)
                model_state = self.model.predict(price_rows, tweet_state, health_state)
                self._render(price_rows, tweet_state, health_state, model_state)
                self._append_log(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "ws_ok": self.prices.ws_ok(),
                        "sse_alive": health_state.get("sse_alive"),
                        "count_source": tweet_state.get("count_source"),
                        "lifetime_count": tweet_state.get("lifetime_count"),
                        "model_sentiment": model_state.get("sentiment"),
                        "model_density_h": model_state.get("tweet_density_per_hour"),
                        "top_prediction": (model_state.get("predictions") or [{}])[0],
                    }
                )
            except KeyboardInterrupt:
                print("\nStopped")
                return
            except Exception as exc:
                print(f"\n[logger-error] {type(exc).__name__}: {exc}")

            elapsed = time.time() - t0
            time.sleep(max(0.2, self.tick_s - elapsed))

    def _render(
        self,
        price_rows: list[dict[str, Any]],
        tweet_state: dict[str, Any],
        health_state: dict[str, Any],
        model_state: dict[str, Any],
    ) -> None:
        pred_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for p in model_state.get("predictions") or []:
            pred_by_key[(str(p.get("event_slug")), str(p.get("label")))] = p

        latest_tweet = tweet_state.get("latest_tweet") or {}
        latest_text = str(latest_tweet.get("tweet_text") or "").replace("\n", " ")
        latest_text = latest_text[:180]

        print("\033[H\033[J", end="")
        print("=" * 128)
        print(
            f"POLYBOT LITE  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC  "
            f"| ws_ok={self.prices.ws_ok()}  sse_alive={health_state.get('sse_alive')}  "
            f"health={health_state.get('status')}"
        )
        print("=" * 128)
        print(
            f"tweet_count={tweet_state.get('lifetime_count')}"
            f" source={tweet_state.get('count_source')}"
            f" age={tweet_state.get('count_age_s')}s"
            f" | sentiment={model_state.get('sentiment')}"
            f" density_h={model_state.get('tweet_density_per_hour')}"
            f" dataset={model_state.get('dataset_rows')}"
        )
        print(
            f"latest_tweet_time={_fmt_ts(latest_tweet.get('tweet_created_at') or latest_tweet.get('detected_at'))}"
            f" | latest_tweet_id={latest_tweet.get('tweet_id')}"
        )
        print(f"latest_tweet_text={latest_text or 'n/a'}")
        print("-" * 128)
        print(
            "EVENT (full) | BRACKET  YES(buy/sell) NO(buy/sell) CUR_CNT  MODEL_P  MKT_P    EDGE    SIGNAL"
        )
        print("-" * 128)

        for row in price_rows:
            slug = str(row.get("event_slug") or "")
            label = str(row.get("label") or "")
            pred = pred_by_key.get((slug, label), {})
            print(
                f"{slug} | {label:>7}  "
                f"{_fmt_price(row.get('yes_buy')):>5}/{_fmt_price(row.get('yes_sell')):<5} "
                f"{_fmt_price(row.get('no_buy')):>5}/{_fmt_price(row.get('no_sell')):<5} "
                f"{str(pred.get('current_count', 'n/a')):>7} "
                f"{str(round(float(pred.get('model_p_yes')), 4)) if pred.get('model_p_yes') is not None else 'n/a':>8} "
                f"{str(round(float(pred.get('market_p_yes')), 4)) if pred.get('market_p_yes') is not None else 'n/a':>6} "
                f"{str(round(float(pred.get('edge')), 4)) if pred.get('edge') is not None else 'n/a':>7} "
                f"{str(pred.get('signal') or 'n/a'):>8}"
            )

        print("-" * 128)
        top = (model_state.get("predictions") or [])[:5]
        if top:
            print("TOP EDGES:")
            for p in top:
                print(
                    f"  {p.get('event_slug')} {p.get('label')} edge={p.get('edge')} signal={p.get('signal')} "
                    f"count={p.get('current_count')} lam={p.get('lambda_remaining')}"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminal logger for Polybot Lite")
    parser.add_argument("--tick-s", type=float, default=3.0, help="Refresh seconds")
    parser.add_argument("--log", default="lite_terminal_events.jsonl", help="JSONL output")
    parser.add_argument("--log-max-lines", type=int, default=400)
    parser.add_argument("--log-max-bytes", type=int, default=500000)
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    TerminalLogger(
        tick_s=args.tick_s,
        out_log=args.log,
        log_max_lines=args.log_max_lines,
        log_max_bytes=args.log_max_bytes,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
