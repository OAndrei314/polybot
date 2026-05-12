from __future__ import annotations

import argparse
import csv
import gzip
import glob
import json
import os
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market.prices import PolymarketPrices
from model.predictor import PredictionModel
from research.parameter_sweep import baseline_params
from signals.health import XTrackerHealth
from signals.stream import TweetStream
from terminal.paper_trade import PaperTradeTerminal


PARAM_DEFAULTS = {
    "bankroll": 1000.0,
    "risk_per_trade_pct": 0.06,
    "max_positions": 8,
    "max_event_exposure_pct": 0.40,
    "entry_edge": 0.08,
    "exit_edge": 0.015,
    "take_profit": 0.30,
    "stop_loss": 0.30,
    "key_stop_loss_cooldown_s": 7200.0,
    "event_stop_loss_cooldown_s": 3600.0,
    "signal_flip_min_edge_gap": 0.06,
    "signal_flip_max_loss_pct": 0.03,
    "key_lock_streak_count": 2,
    "key_lock_duration_s": 21600.0,
    "min_entry_price": 0.04,
    "min_trade_cost": 25.0,
    "low_price_threshold": 0.05,
    "low_price_max_cost": 25.0,
    "micro_price_threshold": 0.20,
    "micro_price_max_cost": 60.0,
    "fee_category": "mentions",
    "fee_rate": None,
    "slippage_rate": 0.005,
    "spread_floor_multiplier": 0.7,
    "uncertainty": 0.05,
    "nb_phi": None,
    "nb_phi_scale": 1.0,
    "hawkes_alpha": None,
    "hawkes_alpha_scale": 1.0,
    "hawkes_beta": 0.18,
    "rate_prior_strength_h": 12.0,
    "stop_loss_dwell_s": 600.0,
    "entry_alignment_max_gap": 0.55,
    "position_size_disagreement_penalty": 1.5,
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _to_float(raw: Any, default: float | None = None) -> float | None:
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _to_int(raw: Any, default: int) -> int:
    try:
        return int(float(raw))
    except Exception:
        return default


def _boolish(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "y"}


def latest_sweep_run(base_dir: str = "data/reports/sweeps") -> Path | None:
    root = Path(base_dir)
    if not root.exists():
        return None
    runs = sorted([p for p in root.iterdir() if p.is_dir()])
    return runs[-1] if runs else None


def normalize_params(raw: dict[str, Any], competitor_id: str | None = None) -> dict[str, Any]:
    params = dict(PARAM_DEFAULTS)
    for key, value in raw.items():
        clean = key[6:] if key.startswith("param_") else key
        if clean in params:
            params[clean] = value

    params["competitor_id"] = str(competitor_id or raw.get("competitor_id") or "competitor")
    for key in [
        "bankroll",
        "risk_per_trade_pct",
        "max_event_exposure_pct",
        "entry_edge",
        "exit_edge",
        "take_profit",
        "stop_loss",
        "key_stop_loss_cooldown_s",
        "event_stop_loss_cooldown_s",
        "signal_flip_min_edge_gap",
        "signal_flip_max_loss_pct",
        "key_lock_duration_s",
        "min_entry_price",
        "min_trade_cost",
        "low_price_threshold",
        "low_price_max_cost",
        "micro_price_threshold",
        "micro_price_max_cost",
        "fee_rate",
        "slippage_rate",
        "spread_floor_multiplier",
        "uncertainty",
        "nb_phi",
        "nb_phi_scale",
        "hawkes_alpha",
        "hawkes_alpha_scale",
        "hawkes_beta",
        "rate_prior_strength_h",
        "stop_loss_dwell_s",
        "entry_alignment_max_gap",
        "position_size_disagreement_penalty",
    ]:
        params[key] = _to_float(params.get(key), PARAM_DEFAULTS.get(key))  # type: ignore[arg-type]
    params["max_positions"] = _to_int(params.get("max_positions"), 8)
    params["key_lock_streak_count"] = _to_int(params.get("key_lock_streak_count"), 2)
    params["fee_category"] = str(params.get("fee_category") or "mentions")
    return params


def load_competitors(args: argparse.Namespace) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []

    if args.config:
        for item in args.config:
            path = Path(item)
            data = json.loads(path.read_text())
            params = data.get("params") if isinstance(data, dict) else None
            if not isinstance(params, dict):
                raise ValueError(f"Config missing params object: {path}")
            configs.append(normalize_params(params, data.get("competitor_id") or path.stem))

    if args.configs_glob:
        for raw_path in sorted(glob.glob(args.configs_glob)):
            path = Path(raw_path)
            data = json.loads(path.read_text())
            params = data.get("params") if isinstance(data, dict) else None
            if isinstance(params, dict):
                configs.append(normalize_params(params, data.get("competitor_id") or path.stem))

    leaderboard_csv = Path(args.leaderboard_csv) if args.leaderboard_csv else None
    top10_json = Path(args.top10_json) if args.top10_json else None
    if not configs and not leaderboard_csv and not top10_json:
        latest = latest_sweep_run(args.sweeps_dir)
        if latest is not None:
            leaderboard_csv = latest / "leaderboard_dev_all_competitors.csv"

    if leaderboard_csv and leaderboard_csv.exists():
        with leaderboard_csv.open() as f:
            for row in csv.DictReader(f):
                cid = str(row.get("competitor_id") or "competitor")
                if cid == "baseline" and not args.include_baseline:
                    continue
                configs.append(normalize_params(row, cid))

    if top10_json and top10_json.exists():
        data = json.loads(top10_json.read_text())
        if not isinstance(data, list):
            raise ValueError(f"Top10 JSON must be a list: {top10_json}")
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("params"), dict):
                continue
            configs.append(normalize_params(item["params"], item.get("competitor_id")))

    if args.include_baseline and not any(c["competitor_id"] == "baseline" for c in configs):
        configs.insert(0, normalize_params(asdict(baseline_params()), "baseline"))

    deduped: dict[str, dict[str, Any]] = {}
    for cfg in configs:
        deduped[str(cfg["competitor_id"])] = cfg
    out = list(deduped.values())
    if not out:
        raise ValueError("No competitors loaded. Pass --config, --configs-glob, --leaderboard-csv, or --top10-json.")
    return out


def seed_dataset(src: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src and os.path.exists(src):
        shutil.copyfile(src, dst)
    else:
        dst.write_text("")


def build_account(cfg: dict[str, Any], run_dir: Path, dataset_seed: str) -> PaperTradeTerminal:
    cid = str(cfg["competitor_id"])
    log_path = run_dir / "competitors" / cid / "trades.jsonl"
    picks_path = run_dir / "competitors" / cid / "latest_picks.jsonl"
    dataset_path = run_dir / "competitors" / cid / "tweet_dataset.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    seed_dataset(dataset_seed, dataset_path)

    account = PaperTradeTerminal(
        tick_s=1.0,
        bankroll=float(cfg["bankroll"]),
        risk_per_trade_pct=float(cfg["risk_per_trade_pct"]),
        max_positions=int(cfg["max_positions"]),
        max_event_exposure_pct=float(cfg["max_event_exposure_pct"]),
        entry_edge=float(cfg["entry_edge"]),
        exit_edge=float(cfg["exit_edge"]),
        take_profit_pct=float(cfg["take_profit"]),
        stop_loss_pct=float(cfg["stop_loss"]),
        stop_loss_dwell_s=float(cfg["stop_loss_dwell_s"]),
        entry_alignment_max_gap=float(cfg["entry_alignment_max_gap"]),
        position_size_disagreement_penalty=float(cfg["position_size_disagreement_penalty"]),
        stop_loss_cooldown_s=float(cfg["key_stop_loss_cooldown_s"]),
        event_stop_loss_cooldown_s=float(cfg["event_stop_loss_cooldown_s"]),
        signal_flip_min_edge_gap=float(cfg["signal_flip_min_edge_gap"]),
        signal_flip_max_loss_pct=float(cfg["signal_flip_max_loss_pct"]),
        key_loss_streak_lock_count=int(cfg["key_lock_streak_count"]),
        key_loss_lock_s=float(cfg["key_lock_duration_s"]),
        min_entry_price=float(cfg["min_entry_price"]),
        min_trade_cost=float(cfg["min_trade_cost"]),
        low_price_threshold=float(cfg["low_price_threshold"]),
        low_price_max_cost=float(cfg["low_price_max_cost"]),
        micro_price_threshold=float(cfg["micro_price_threshold"]),
        micro_price_max_cost=float(cfg["micro_price_max_cost"]),
        require_twitter_ok_for_entries=True,
        hedge_enabled=False,
        fee_category=str(cfg["fee_category"]),
        fee_rate=cfg.get("fee_rate"),
        slippage_rate=float(cfg["slippage_rate"]),
        spread_floor_multiplier=float(cfg["spread_floor_multiplier"]),
        uncertainty=float(cfg["uncertainty"]),
        rate_prior_strength_h=float(cfg["rate_prior_strength_h"]),
        nb_phi_override=cfg.get("nb_phi"),
        nb_phi_scale=float(cfg["nb_phi_scale"]),
        hawkes_alpha_scale=float(cfg["hawkes_alpha_scale"]),
        hawkes_alpha=cfg.get("hawkes_alpha"),
        hawkes_beta_per_min=float(cfg["hawkes_beta"]),
        log_path=str(log_path),
        picks_log_path=str(picks_path),
        picks_overwrite_latest=True,
        log_max_lines=2_000_000,
        log_max_bytes=500_000_000,
    )
    account.model = PredictionModel(
        dataset_path=str(dataset_path),
        rate_prior_strength_h=float(cfg["rate_prior_strength_h"]),
        nb_phi_override=cfg.get("nb_phi"),
        nb_phi_scale=float(cfg["nb_phi_scale"]),
        hawkes_alpha_scale=float(cfg["hawkes_alpha_scale"]),
        hawkes_beta_per_min=float(cfg["hawkes_beta"]),
    )
    if cfg.get("hawkes_alpha") is not None:
        # Rebuild through the terminal constructor semantics so absolute alpha
        # is respected consistently with `run.py paper --hawkes-alpha`.
        account.model = PredictionModel(
            dataset_path=str(dataset_path),
            rate_prior_strength_h=float(cfg["rate_prior_strength_h"]),
            nb_phi_override=cfg.get("nb_phi"),
            nb_phi_scale=float(cfg["nb_phi_scale"]),
            hawkes_alpha_scale=account.hawkes_alpha_scale,
            hawkes_beta_per_min=float(cfg["hawkes_beta"]),
        )
    return account


def parse_trade_log(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


class DailyStats:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)

    def update(self, cid: str, day: str, equity: float) -> None:
        stat = self.rows[cid].get(day)
        if stat is None:
            stat = {
                "start_equity": equity,
                "end_equity": equity,
                "high_equity": equity,
                "low_equity": equity,
                "max_drawdown": 0.0,
                "samples": 0.0,
            }
            self.rows[cid][day] = stat
        stat["end_equity"] = equity
        stat["high_equity"] = max(stat["high_equity"], equity)
        stat["low_equity"] = min(stat["low_equity"], equity)
        if stat["high_equity"] > 0:
            stat["max_drawdown"] = max(
                stat["max_drawdown"],
                (stat["high_equity"] - equity) / stat["high_equity"],
            )
        stat["samples"] += 1.0

    def summary(self, cid: str) -> list[dict[str, Any]]:
        out = []
        for day, stat in sorted(self.rows.get(cid, {}).items()):
            start = stat["start_equity"]
            end = stat["end_equity"]
            out.append(
                {
                    "day": day,
                    "start_equity": start,
                    "end_equity": end,
                    "day_pnl": end - start,
                    "day_return": (end / start - 1.0) if start > 0 else 0.0,
                    "max_drawdown": stat["max_drawdown"],
                    "samples": int(stat["samples"]),
                }
            )
        return out


def portfolio_snapshot(
    account: PaperTradeTerminal,
    cid: str,
    price_by_key: dict[str, dict[str, Any]],
    ts: datetime,
) -> dict[str, Any]:
    invested = sum(float(pos.get("cost") or 0.0) for pos in account.positions.values())
    unrealized = account._unrealized(price_by_key)
    equity = account.cash + invested + unrealized
    return {
        "ts": ts.isoformat(),
        "competitor_id": cid,
        "cash": account.cash,
        "invested": invested,
        "unrealized_pnl": unrealized,
        "realized_pnl": account.realized_pnl,
        "equity": equity,
        "total_pnl": equity - account.bankroll,
        "open_positions": len(account.positions),
        "closed_count": account.closed_count,
    }


def metrics_from_account(
    account: PaperTradeTerminal,
    cid: str,
    price_by_key: dict[str, dict[str, Any]],
    daily: DailyStats,
) -> dict[str, Any]:
    snap = portfolio_snapshot(account, cid, price_by_key, _now_utc())
    trades = parse_trade_log(account.log_path)
    sells = [r for r in trades if str(r.get("action") or "").upper() == "SELL"]
    buys = [r for r in trades if str(r.get("action") or "").upper() == "BUY"]
    by_key: dict[str, float] = defaultdict(float)
    for row in sells:
        key = f"{row.get('event_slug')}|{row.get('label')}|{row.get('side')}"
        by_key[key] += float(row.get("pnl") or 0.0)
    positive_total = sum(max(0.0, v) for v in by_key.values())
    top_key_share = (
        max((max(0.0, v) for v in by_key.values()), default=0.0) / positive_total
        if positive_total > 0
        else 0.0
    )
    reason_counts = Counter(str(r.get("reason") or "") for r in sells)
    daily_rows = daily.summary(cid)
    worst_day = min((float(r["day_pnl"]) for r in daily_rows), default=0.0)
    max_day_dd = max((float(r["max_drawdown"]) for r in daily_rows), default=0.0)
    return {
        **snap,
        "buys": len(buys),
        "sells": len(sells),
        "wins": sum(1 for r in sells if float(r.get("pnl") or 0.0) > 0),
        "losses": sum(1 for r in sells if float(r.get("pnl") or 0.0) < 0),
        "stop_loss_count": reason_counts.get("stop_loss", 0),
        "stop_loss_churn": reason_counts.get("stop_loss", 0) / max(1, len(sells)),
        "unique_event_brackets": len(by_key),
        "top_key_pnl_share": top_key_share,
        "worst_day_pnl": worst_day,
        "max_daily_drawdown": max_day_dd,
        "days": daily_rows,
    }


def write_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, default=_json_default) + "\n")


def write_gzip_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "at") as f:
        f.write(json.dumps(row, default=_json_default) + "\n")


def write_leaderboards(
    run_dir: Path,
    accounts: dict[str, PaperTradeTerminal],
    price_by_key: dict[str, dict[str, Any]],
    daily: DailyStats,
) -> list[dict[str, Any]]:
    rows = [
        metrics_from_account(account, cid, price_by_key, daily)
        for cid, account in accounts.items()
    ]
    rows.sort(
        key=lambda r: (
            float(r["total_pnl"]),
            -float(r["max_daily_drawdown"]),
            -float(r["top_key_pnl_share"]),
        ),
        reverse=True,
    )
    payload = {
        "ts": _now_utc().isoformat(),
        "leaderboard": rows,
    }
    (run_dir / "leaderboard_latest.json").write_text(json.dumps(payload, indent=2, default=_json_default) + "\n")
    csv_path = run_dir / "leaderboard_latest.csv"
    flat_rows = []
    for row in rows:
        flat = dict(row)
        flat.pop("days", None)
        flat_rows.append(flat)
    if flat_rows:
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(flat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(flat_rows)
    write_jsonl(run_dir / "leaderboard_history.jsonl", payload)
    daily_payload = {
        "ts": payload["ts"],
        "days": {cid: daily.summary(cid) for cid in accounts},
    }
    (run_dir / "daily_metrics_latest.json").write_text(
        json.dumps(daily_payload, indent=2, default=_json_default) + "\n"
    )
    write_jsonl(run_dir / "daily_metrics_history.jsonl", daily_payload)
    return rows


def run_live_test(args: argparse.Namespace) -> dict[str, Any]:
    competitors = load_competitors(args)
    started = _now_utc()
    run_dir = Path(args.out_dir) / started.strftime("%Y%m%d_%H%M%S_real_48h")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "competitors.json").write_text(json.dumps(competitors, indent=2) + "\n")

    accounts = {
        str(cfg["competitor_id"]): build_account(cfg, run_dir, args.dataset_seed)
        for cfg in competitors
    }

    prices = PolymarketPrices()
    tweets = TweetStream()
    health = XTrackerHealth()
    prices.start()
    tweets.start()

    duration_s = max(0.0, float(args.duration_h) * 3600.0)
    end_unix = time.time() + duration_s
    next_report = 0.0
    next_archive = 0.0
    next_health_poll = 0.0
    health_state: dict = {}
    daily = DailyStats()

    summary = {
        "run_dir": str(run_dir),
        "started_at_utc": started.isoformat(),
        "duration_h": args.duration_h,
        "tick_s": args.tick_s,
        "competitors": list(accounts.keys()),
        "real_data_only": True,
        "allow_synthetic_price_fallback": bool(args.allow_synthetic_price_fallback),
        "input_archive": str(run_dir / "real_input_ticks.jsonl.gz"),
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(summary, indent=2) + "\n")

    try:
        while time.time() < end_unix:
            loop_start = time.time()
            ts = _now_utc()
            prices.refresh_event_universe()
            health.set_event_slugs(list(prices.event_slugs))
            feed_max_silence_s = max(10.0, args.tick_s * 2.0)
            ws_ok_now = prices.ws_ok(max_silence_s=feed_max_silence_s)
            prices.refresh_stale_tokens(force_all=not ws_ok_now)
            price_rows = prices.snapshot(
                allow_synthetic_fallback=bool(args.allow_synthetic_price_fallback)
            )
            price_feed_state = prices.feed_state(max_silence_s=feed_max_silence_s)
            synthetic_price_rows = sum(1 for r in price_rows if r.get("is_synthetic_price"))
            tweet_state_base = tweets.poll()
            if time.time() >= next_health_poll:
                health_state = health.poll(tweet_state_base)
                next_health_poll = time.time() + max(5.0, float(args.health_poll_s))
            price_by_key = {
                PaperTradeTerminal._key(str(r.get("event_slug")), str(r.get("label"))): r
                for r in price_rows
            }

            if not args.no_input_archive and time.time() >= next_archive:
                write_gzip_jsonl(
                    run_dir / "real_input_ticks.jsonl.gz",
                    {
                        "ts": ts.isoformat(),
                        "price_rows": price_rows,
                        "price_feed_state": price_feed_state,
                        "synthetic_price_rows": synthetic_price_rows,
                        "pre_account_loop_elapsed_s": round(time.time() - loop_start, 3),
                        "tweet_state": tweet_state_base,
                        "health_state": health_state,
                        "event_slugs": list(prices.event_slugs),
                    },
                )
                next_archive = time.time() + max(1.0, float(args.input_archive_s))

            for cid, account in accounts.items():
                tweet_state = dict(tweet_state_base)
                tweet_state["event_live_counts"] = account._build_event_live_counts(tweet_state, health_state)
                model_state = account.model.predict(price_rows, tweet_state, health_state)
                enriched: list[dict[str, Any]] = []
                for base_pred in model_state.get("predictions") or []:
                    key = PaperTradeTerminal._key(
                        str(base_pred.get("event_slug")),
                        str(base_pred.get("label")),
                    )
                    row = price_by_key.get(key)
                    if row:
                        enriched.append(account._with_true_edges(base_pred, row))
                pred_by_key = {
                    PaperTradeTerminal._key(str(p.get("event_slug")), str(p.get("label"))): p
                    for p in enriched
                }
                now_unix = time.time()
                account._manage_positions(price_by_key, pred_by_key, now_unix)
                if not account._twitter_ok_for_entries(tweet_state, health_state):
                    for pred in pred_by_key.values():
                        if str(pred.get("pick_signal") or "") in ("BUY_YES", "BUY_NO"):
                            pred["pick_signal"] = "HOLD"
                            pred["entry_block_reason"] = pred.get("entry_block_reason") or "twitter_stream_off"
                account._try_entries(price_by_key, pred_by_key, now_unix)
                snap = portfolio_snapshot(account, cid, price_by_key, ts)
                daily.update(cid, ts.date().isoformat(), float(snap["equity"]))
                write_jsonl(run_dir / "portfolio_snapshots.jsonl", snap)

            if time.time() >= next_report:
                leaderboard = write_leaderboards(run_dir, accounts, price_by_key, daily)
                print(
                    f"{ts.isoformat()} arena={len(accounts)} "
                    f"best={leaderboard[0]['competitor_id']} pnl={leaderboard[0]['total_pnl']:+.2f} "
                    f"open={sum(len(a.positions) for a in accounts.values())}"
                )
                next_report = time.time() + max(5.0, float(args.report_s))

            if args.once:
                break
            elapsed = time.time() - loop_start
            write_jsonl(
                run_dir / "loop_metrics.jsonl",
                {
                    "ts": ts.isoformat(),
                    "loop_elapsed_s": round(elapsed, 3),
                    "tick_s": float(args.tick_s),
                    "account_count": len(accounts),
                    "price_rows": len(price_rows),
                    "synthetic_price_rows": synthetic_price_rows,
                    "price_feed_state": price_feed_state,
                },
            )
            time.sleep(max(0.2, float(args.tick_s) - elapsed))
    finally:
        try:
            prices.stop()
        except Exception:
            pass

    final_rows = write_leaderboards(run_dir, accounts, price_by_key if "price_by_key" in locals() else {}, daily)
    final = {
        **summary,
        "ended_at_utc": _now_utc().isoformat(),
        "final_leaderboard": final_rows,
    }
    (run_dir / "final_report.json").write_text(json.dumps(final, indent=2, default=_json_default) + "\n")
    return final


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live real-data competitor paper-trading arena")
    parser.add_argument("--duration-h", type=float, default=48.0)
    parser.add_argument("--tick-s", type=float, default=10.0)
    parser.add_argument("--report-s", type=float, default=300.0)
    parser.add_argument("--input-archive-s", type=float, default=30.0)
    parser.add_argument(
        "--health-poll-s",
        type=float,
        default=60.0,
        help="Throttle XTracker health.poll() to at most once every N seconds.",
    )
    parser.add_argument("--out-dir", default="data/reports/live_competitor_tests")
    parser.add_argument("--sweeps-dir", default="data/reports/sweeps")
    parser.add_argument("--dataset-seed", default="tweet_dataset.jsonl")
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--configs-glob", default="")
    parser.add_argument("--leaderboard-csv", default="")
    parser.add_argument("--top10-json", default="")
    parser.add_argument("--include-baseline", action="store_true", default=True)
    parser.add_argument("--no-baseline", dest="include_baseline", action="store_false")
    parser.add_argument("--no-input-archive", action="store_true")
    parser.add_argument(
        "--allow-synthetic-price-fallback",
        action="store_true",
        help="Allow Gamma midpoint fallback for empty CLOB books. Leave off for strict real-BBO tests.",
    )
    parser.add_argument("--once", action="store_true", help="Run exactly one live tick for smoke testing.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    final = run_live_test(args)
    print(json.dumps({"run_dir": final["run_dir"], "ended_at_utc": final["ended_at_utc"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
