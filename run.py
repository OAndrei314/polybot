from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict
from typing import Any

from terminal.paper_trade import PaperTradeTerminal
from model.predictor import PredictionModel
from market.prices import PolymarketPrices
from terminal.logger import TerminalLogger
from signals.stream import TweetStream
from signals.health import XTrackerHealth
from ingestion.coverage_report import build_report, print_report, save_report
from model.build_training_data import build_and_save_features


def cmd_prices() -> int:
    feed = PolymarketPrices()
    feed.start()
    time.sleep(2)
    feed.refresh_stale_tokens(force_all=True)
    rows = feed.snapshot()
    print(json.dumps(rows, indent=2))
    return 0


def cmd_tweets() -> int:
    stream = TweetStream()
    stream.start()
    for _ in range(3):
        print(json.dumps(stream.poll(), indent=2))
        time.sleep(3)
    return 0


def cmd_health() -> int:
    stream = TweetStream()
    stream.start()
    checker = XTrackerHealth()
    print(json.dumps(checker.poll(stream.poll()), indent=2))
    return 0


def cmd_model() -> int:
    feed = PolymarketPrices()
    feed.start()
    stream = TweetStream()
    stream.start()
    checker = XTrackerHealth()
    model = PredictionModel()

    time.sleep(2)
    feed.refresh_stale_tokens(force_all=True)

    prices = feed.snapshot()
    tweets = stream.poll()
    health = checker.poll(tweets)
    out = model.predict(prices, tweets, health)
    print(json.dumps(out, indent=2))
    return 0


def cmd_coverage(cache: str, start: str, end: str | None, out_dir: str) -> int:
    try:
        import pandas as pd
    except Exception as exc:
        print(f"pandas import failed: {exc}")
        return 1

    if not os.path.exists(cache):
        print(f"dataset not found: {cache}")
        return 1

    df = pd.read_parquet(cache)
    report = build_report(df, start=start, end=end)
    print_report(report)
    save_report(report, out_dir)
    print(f"\nSaved CSV/JSON outputs to {out_dir}")
    return 0


def cmd_features(refresh: bool, out_dir: str) -> int:
    build_and_save_features(refresh=refresh, out_dir=out_dir)
    return 0


def cmd_live(tick_s: float) -> int:
    TerminalLogger(tick_s=tick_s).run()
    return 0


def cmd_paper(
    tick_s: float,
    bankroll: float,
    risk_per_trade: float,
    max_positions: int,
    max_event_exposure_pct: float,
    entry_edge: float,
    exit_edge: float,
    take_profit: float,
    stop_loss: float,
    cooldown_s: float,
    stop_loss_cooldown_s: float,
    ended_count_change_cooldown_s: float,
    signal_flip_min_edge_gap: float,
    signal_flip_max_loss_pct: float,
    event_stop_loss_cooldown_s: float,
    stop_loss_reentry_edge_buffer: float,
    key_loss_streak_lock_count: int,
    key_loss_lock_s: float,
    min_entry_price: float,
    min_trade_cost: float,
    low_price_threshold: float,
    low_price_max_cost: float,
    micro_price_threshold: float,
    micro_price_max_cost: float,
    require_twitter_ok_for_entries: bool,
    hedge_enabled: bool,
    hedge_ratio: float,
    max_hedges_per_primary: int,
    hedge_min_true_edge: float,
    fee_category: str,
    fee_rate: float | None,
    slippage_rate: float,
    spread_floor_multiplier: float,
    short_hold_fee_window_s: float,
    short_hold_extra_fee_rate: float,
    uncertainty: float,
    rate_prior_strength_h: float,
    nb_phi_override: float | None,
    nb_phi_scale: float,
    hawkes_alpha_scale: float,
    hawkes_alpha: float | None,
    hawkes_beta_per_min: float,
    log_path: str,
    picks_log_path: str,
    log_max_lines: int,
    log_max_bytes: int,
    picks_overwrite_latest: bool,
    picks_max_lines: int,
    picks_max_bytes: int,
    stale_refresh_interval_s: float,
    ws_down_force_refresh_s: float,
) -> int:
    PaperTradeTerminal(
        tick_s=tick_s,
        bankroll=bankroll,
        risk_per_trade_pct=risk_per_trade,
        max_positions=max_positions,
        max_event_exposure_pct=max_event_exposure_pct,
        entry_edge=entry_edge,
        exit_edge=exit_edge,
        take_profit_pct=take_profit,
        stop_loss_pct=stop_loss,
        cooldown_s=cooldown_s,
        stop_loss_cooldown_s=stop_loss_cooldown_s,
        ended_count_change_cooldown_s=ended_count_change_cooldown_s,
        signal_flip_min_edge_gap=signal_flip_min_edge_gap,
        signal_flip_max_loss_pct=signal_flip_max_loss_pct,
        event_stop_loss_cooldown_s=event_stop_loss_cooldown_s,
        stop_loss_reentry_edge_buffer=stop_loss_reentry_edge_buffer,
        key_loss_streak_lock_count=key_loss_streak_lock_count,
        key_loss_lock_s=key_loss_lock_s,
        min_entry_price=min_entry_price,
        min_trade_cost=min_trade_cost,
        low_price_threshold=low_price_threshold,
        low_price_max_cost=low_price_max_cost,
        micro_price_threshold=micro_price_threshold,
        micro_price_max_cost=micro_price_max_cost,
        require_twitter_ok_for_entries=require_twitter_ok_for_entries,
        hedge_enabled=hedge_enabled,
        hedge_ratio=hedge_ratio,
        max_hedges_per_primary=max_hedges_per_primary,
        hedge_min_true_edge=hedge_min_true_edge,
        fee_category=fee_category,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        spread_floor_multiplier=spread_floor_multiplier,
        short_hold_fee_window_s=short_hold_fee_window_s,
        short_hold_extra_fee_rate=short_hold_extra_fee_rate,
        uncertainty=uncertainty,
        rate_prior_strength_h=rate_prior_strength_h,
        nb_phi_override=nb_phi_override,
        nb_phi_scale=nb_phi_scale,
        hawkes_alpha_scale=hawkes_alpha_scale,
        hawkes_alpha=hawkes_alpha,
        hawkes_beta_per_min=hawkes_beta_per_min,
        log_path=log_path,
        picks_log_path=picks_log_path,
        log_max_lines=log_max_lines,
        log_max_bytes=log_max_bytes,
        picks_overwrite_latest=picks_overwrite_latest,
        picks_max_lines=picks_max_lines,
        picks_max_bytes=picks_max_bytes,
        stale_refresh_interval_s=stale_refresh_interval_s,
        ws_down_force_refresh_s=ws_down_force_refresh_s,
    ).run()
    return 0


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def cmd_summary(log_path: str, last: int = 30, event_breakdown: bool = True) -> int:
    path = os.path.abspath(log_path)
    if not os.path.exists(path):
        print(f"log file not found: {path}")
        return 1

    rows: list[dict[str, Any]] = []
    skipped = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                skipped += 1
                continue
            if isinstance(row, dict):
                rows.append(row)

    buys = [r for r in rows if str(r.get("action") or "").upper() == "BUY"]
    sells = [r for r in rows if str(r.get("action") or "").upper() == "SELL"]

    wins = [r for r in sells if (_to_float(r.get("pnl")) or 0.0) > 0.0]
    losses = [r for r in sells if (_to_float(r.get("pnl")) or 0.0) < 0.0]
    flats = [r for r in sells if (_to_float(r.get("pnl")) or 0.0) == 0.0]

    realized = sum(_to_float(r.get("pnl")) or 0.0 for r in sells)
    gross_win = sum(max(0.0, _to_float(r.get("pnl")) or 0.0) for r in sells)
    gross_loss = sum(min(0.0, _to_float(r.get("pnl")) or 0.0) for r in sells)
    win_rate = (len(wins) / len(sells) * 100.0) if sells else 0.0

    first_ts = rows[0].get("ts") if rows else None
    last_ts = rows[-1].get("ts") if rows else None

    print("PAPER TRADE SUMMARY")
    print("-" * 120)
    print(f"log_file={path}")
    print(
        f"rows={len(rows)} skipped={skipped} buys={len(buys)} sells={len(sells)} "
        f"wins={len(wins)} losses={len(losses)} flats={len(flats)}"
    )
    print(
        f"realized_pnl={realized:+.3f} gross_win={gross_win:+.3f} gross_loss={gross_loss:+.3f} "
        f"win_rate={win_rate:.2f}%"
    )
    print(f"time_range={first_ts or 'n/a'} -> {last_ts or 'n/a'}")

    if sells:
        best = max(sells, key=lambda r: _to_float(r.get("pnl")) or 0.0)
        worst = min(sells, key=lambda r: _to_float(r.get("pnl")) or 0.0)
        print(
            f"best_close={best.get('ts')} {best.get('event_slug')} {best.get('label')} "
            f"{best.get('side')} pnl={_to_float(best.get('pnl')) or 0.0:+.3f} reason={best.get('reason')}"
        )
        print(
            f"worst_close={worst.get('ts')} {worst.get('event_slug')} {worst.get('label')} "
            f"{worst.get('side')} pnl={_to_float(worst.get('pnl')) or 0.0:+.3f} reason={worst.get('reason')}"
        )

    if event_breakdown and sells:
        by_event: dict[str, float] = defaultdict(float)
        for r in sells:
            by_event[str(r.get("event_slug") or "n/a")] += _to_float(r.get("pnl")) or 0.0
        print("-" * 120)
        print("P&L BY EVENT")
        event_w = max(len("EVENT"), max((len(k) for k in by_event.keys()), default=5))
        print(f"{'EVENT':<{event_w}} {'REALIZED_PNL':>14}")
        for event_slug, pnl in sorted(by_event.items(), key=lambda kv: kv[1], reverse=True):
            print(f"{event_slug:<{event_w}} {pnl:>14.3f}")

    print("-" * 120)
    print("RECENT CLOSES")
    recent = sells[-max(0, int(last)) :] if sells else []
    if not recent:
        print("no closed trades yet")
        return 0

    event_w = max(len("EVENT"), max((len(str(r.get("event_slug") or "")) for r in recent), default=5))
    print(
        f"{'TS':<32} {'EVENT':<{event_w}} {'BRKT':>7} {'SIDE':>4} "
        f"{'PNL':>10} {'REASON':>12}"
    )
    for r in recent:
        print(
            f"{str(r.get('ts') or 'n/a'):<32} "
            f"{str(r.get('event_slug') or 'n/a'):<{event_w}} "
            f"{str(r.get('label') or 'n/a'):>7} "
            f"{str(r.get('side') or 'n/a'):>4} "
            f"{(_to_float(r.get('pnl')) or 0.0):>10.3f} "
            f"{str(r.get('reason') or 'n/a'):>12}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Polybot Lite entrypoint")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("prices", help="One-shot Polymarket prices snapshot")
    sub.add_parser("tweets", help="One-shot tweet streams snapshot")
    sub.add_parser("health", help="One-shot SSE/XTracker health snapshot")
    sub.add_parser("model", help="One-shot model prediction snapshot")
    coverage = sub.add_parser("coverage", help="Tweet history coverage/gap report")
    coverage.add_argument("--cache", default="data/tweets_all.parquet")
    coverage.add_argument("--start", default="2009-01-01T00:00:00Z")
    coverage.add_argument("--end", default=None)
    coverage.add_argument("--out-dir", default="data/reports/coverage")
    features = sub.add_parser("features", help="Build model-ready feature tables")
    features.add_argument("--refresh", action="store_true")
    features.add_argument("--out-dir", default="data/features")
    summary = sub.add_parser("summary", help="Summarize paper-trade outcomes from a JSONL log")
    summary.add_argument("--log", default="paper_trade_events.jsonl")
    summary.add_argument("--last", type=int, default=30)
    summary.add_argument("--no-event-breakdown", action="store_true")

    live = sub.add_parser("live", help="Live terminal logger")
    live.add_argument("--tick-s", type=float, default=3.0)

    paper = sub.add_parser("paper", help="Paper-trade terminal")
    paper.add_argument("--tick-s", type=float, default=8.0)
    paper.add_argument("--bankroll", type=float, default=1000.0)
    paper.add_argument("--risk-per-trade", type=float, default=0.08)
    paper.add_argument("--max-positions", type=int, default=8)
    paper.add_argument("--max-event-exposure-pct", type=float, default=0.45)
    paper.add_argument("--entry-edge", type=float, default=0.08)
    paper.add_argument("--exit-edge", type=float, default=0.015)
    paper.add_argument("--take-profit", type=float, default=0.22)
    paper.add_argument("--stop-loss", type=float, default=0.14)
    paper.add_argument("--cooldown-s", type=float, default=60.0)
    paper.add_argument("--stop-loss-cooldown-s", type=float, default=7200.0)
    paper.add_argument("--ended-count-change-cooldown-s", type=float, default=900.0)
    paper.add_argument("--signal-flip-min-edge-gap", type=float, default=0.06)
    paper.add_argument("--signal-flip-max-loss-pct", type=float, default=0.03)
    paper.add_argument("--event-stop-loss-cooldown-s", type=float, default=3600.0)
    paper.add_argument("--stop-loss-reentry-edge-buffer", type=float, default=0.06)
    paper.add_argument("--key-loss-streak-lock-count", type=int, default=2)
    paper.add_argument("--key-loss-lock-s", type=float, default=21600.0)
    paper.add_argument("--min-entry-price", type=float, default=0.02)
    paper.add_argument("--min-trade-cost", type=float, default=25.0)
    paper.add_argument("--low-price-threshold", type=float, default=0.05)
    paper.add_argument("--low-price-max-cost", type=float, default=25.0)
    paper.add_argument("--micro-price-threshold", type=float, default=0.2)
    paper.add_argument("--micro-price-max-cost", type=float, default=80.0)
    paper.add_argument("--require-twitter-ok-for-entries", action="store_true", default=True)
    paper.add_argument(
        "--allow-entries-without-twitter",
        dest="require_twitter_ok_for_entries",
        action="store_false",
    )
    paper.add_argument("--hedge-enabled", action="store_true", default=True)
    paper.add_argument("--no-hedge", dest="hedge_enabled", action="store_false")
    paper.add_argument("--hedge-ratio", type=float, default=0.35)
    paper.add_argument("--max-hedges-per-primary", type=int, default=1)
    paper.add_argument("--hedge-min-true-edge", type=float, default=0.02)
    paper.add_argument("--fee-category", default="mentions")
    paper.add_argument(
        "--fee-rate",
        type=float,
        default=None,
        help="Override Polymarket taker fee rate; defaults to category table.",
    )
    paper.add_argument("--slippage-rate", type=float, default=0.005)
    paper.add_argument("--spread-floor-multiplier", type=float, default=0.4)
    paper.add_argument("--short-hold-fee-window-s", type=float, default=900.0)
    paper.add_argument("--short-hold-extra-fee-rate", type=float, default=0.01)
    paper.add_argument("--uncertainty", type=float, default=0.05)
    paper.add_argument("--rate-prior-strength-h", type=float, default=24.0)
    paper.add_argument("--nb-phi", dest="nb_phi_override", type=float, default=None)
    paper.add_argument("--nb-phi-scale", type=float, default=1.0)
    paper.add_argument(
        "--hawkes-alpha",
        dest="hawkes_alpha",
        type=float,
        default=None,
        help="Absolute Hawkes excitation alpha; defaults to calibrated live model alpha.",
    )
    paper.add_argument("--hawkes-alpha-scale", type=float, default=1.0)
    paper.add_argument("--hawkes-beta", dest="hawkes_beta_per_min", type=float, default=0.18)
    paper.add_argument("--log", default="paper_trade_events.jsonl")
    paper.add_argument("--picks-log", default="paper_picks_snapshot.jsonl")
    paper.add_argument("--log-max-lines", type=int, default=400)
    paper.add_argument("--log-max-bytes", type=int, default=500000)
    paper.add_argument("--picks-overwrite-latest", action="store_true", default=True)
    paper.add_argument("--picks-append", dest="picks_overwrite_latest", action="store_false")
    paper.add_argument("--picks-max-lines", type=int, default=60)
    paper.add_argument("--picks-max-bytes", type=int, default=300000)
    paper.add_argument("--stale-refresh-interval-s", type=float, default=24.0)
    paper.add_argument("--ws-down-force-refresh-s", type=float, default=8.0)

    bench = sub.add_parser(
        "latency-bench",
        help="Run all tweet-detection methods in parallel and log per-method latency.",
    )
    bench.add_argument("--user-id", default="44196397", help="X user_id (default: elonmusk).")
    bench.add_argument("--out-dir", default="data/signal_latency")
    bench.add_argument("--gql-interval-s", type=float, default=2.0)
    bench.add_argument("--status-s", type=float, default=15.0)
    bench.add_argument("--log-file", default="signal_latency.log")
    bench.add_argument("--no-gql", action="store_true")
    bench.add_argument("--no-sse", action="store_true")
    bench.add_argument("--browser-log", default="tweet_signal_events.jsonl")
    bench.add_argument("--no-browser-observer", action="store_true")

    args = parser.parse_args()
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if args.cmd == "prices":
        return cmd_prices()
    if args.cmd == "tweets":
        return cmd_tweets()
    if args.cmd == "health":
        return cmd_health()
    if args.cmd == "model":
        return cmd_model()
    if args.cmd == "coverage":
        return cmd_coverage(
            cache=args.cache,
            start=args.start,
            end=args.end,
            out_dir=args.out_dir,
        )
    if args.cmd == "features":
        return cmd_features(refresh=args.refresh, out_dir=args.out_dir)
    if args.cmd == "live":
        return cmd_live(args.tick_s)
    if args.cmd == "summary":
        return cmd_summary(
            log_path=args.log,
            last=args.last,
            event_breakdown=not args.no_event_breakdown,
        )
    if args.cmd == "paper":
        return cmd_paper(
            args.tick_s,
            args.bankroll,
            args.risk_per_trade,
            args.max_positions,
            args.max_event_exposure_pct,
            args.entry_edge,
            args.exit_edge,
            args.take_profit,
            args.stop_loss,
            args.cooldown_s,
            args.stop_loss_cooldown_s,
            args.ended_count_change_cooldown_s,
            args.signal_flip_min_edge_gap,
            args.signal_flip_max_loss_pct,
            args.event_stop_loss_cooldown_s,
            args.stop_loss_reentry_edge_buffer,
            args.key_loss_streak_lock_count,
            args.key_loss_lock_s,
            args.min_entry_price,
            args.min_trade_cost,
            args.low_price_threshold,
            args.low_price_max_cost,
            args.micro_price_threshold,
            args.micro_price_max_cost,
            args.require_twitter_ok_for_entries,
            args.hedge_enabled,
            args.hedge_ratio,
            args.max_hedges_per_primary,
            args.hedge_min_true_edge,
            args.fee_category,
            args.fee_rate,
            args.slippage_rate,
            args.spread_floor_multiplier,
            args.short_hold_fee_window_s,
            args.short_hold_extra_fee_rate,
            args.uncertainty,
            args.rate_prior_strength_h,
            args.nb_phi_override,
            args.nb_phi_scale,
            args.hawkes_alpha_scale,
            args.hawkes_alpha,
            args.hawkes_beta_per_min,
            args.log,
            args.picks_log,
            args.log_max_lines,
            args.log_max_bytes,
            args.picks_overwrite_latest,
            args.picks_max_lines,
            args.picks_max_bytes,
            args.stale_refresh_interval_s,
            args.ws_down_force_refresh_s,
        )
    if args.cmd == "latency-bench":
        from signals.latency_bench import main as bench_main
        bench_argv = [
            "--user-id", str(args.user_id),
            "--out-dir", str(args.out_dir),
            "--gql-interval-s", str(args.gql_interval_s),
            "--status-s", str(args.status_s),
            "--log-file", str(args.log_file),
        ]
        if args.no_gql:
            bench_argv.append("--no-gql")
        if args.no_sse:
            bench_argv.append("--no-sse")
        bench_argv += ["--browser-log", str(args.browser_log)]
        if args.no_browser_observer:
            bench_argv.append("--no-browser-observer")
        return bench_main(bench_argv) or 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
