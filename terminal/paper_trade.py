from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from model.fees import FeeModel
from model.hawkes import DEFAULT_ALPHA_ORIG
from model.predictor import PredictionModel
from market.prices import PolymarketPrices
from signals.stream import TweetStream
from signals.health import XTrackerHealth


def _fmt(v: Any, nd: int = 3) -> str:
    if isinstance(v, (int, float)):
        return f"{float(v):.{nd}f}"
    return "n/a"


def _fmt_ts(raw: str | None) -> str:
    if not raw:
        return "n/a"
    return str(raw).replace("T", " ").replace("+00:00", "Z")


def _clamp(v: float, low: float, high: float) -> float:
    return max(low, min(high, v))


def _tail_lines_bytes(path: str, keep_lines: int, chunk_size: int = 65536) -> bytes:
    if keep_lines <= 0:
        return b""
    if not os.path.exists(path):
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
            chunk = f.read(read_size)
            buf = chunk + buf
            line_count = buf.count(b"\n")

    lines = buf.splitlines()[-keep_lines:]
    if not lines:
        return b""
    return b"\n".join(lines) + b"\n"


class PaperTradeTerminal:
    """Simple paper-trade engine + terminal monitor with true-edge execution checks."""

    def __init__(
        self,
        tick_s: float = 8.0,
        bankroll: float = 1_000.0,
        risk_per_trade_pct: float = 0.08,
        max_positions: int = 8,
        max_event_exposure_pct: float = 0.45,
        entry_edge: float = 0.08,
        exit_edge: float = 0.015,
        take_profit_pct: float = 0.22,
        stop_loss_pct: float = 0.25,
        stop_loss_dwell_s: float = 600.0,
        entry_alignment_max_gap: float = 0.55,
        position_size_disagreement_penalty: float = 1.5,
        cooldown_s: float = 60.0,
        stop_loss_cooldown_s: float = 7_200.0,
        count_change_cooldown_s: float = 45.0,
        ended_count_change_cooldown_s: float = 900.0,
        signal_flip_edge_buffer: float = 0.03,
        signal_flip_min_edge_gap: float = 0.06,
        signal_flip_max_loss_pct: float = 0.03,
        signal_flip_min_hold_s: float = 600.0,
        signal_flip_reentry_cooldown_s: float = 1_800.0,
        event_signal_flip_cooldown_s: float = 900.0,
        event_stop_loss_cooldown_s: float = 3_600.0,
        reentry_edge_buffer: float = 0.02,
        stop_loss_reentry_edge_buffer: float = 0.06,
        key_loss_streak_lock_count: int = 2,
        key_loss_lock_s: float = 21_600.0,
        min_entry_price: float = 0.02,
        min_trade_cost: float = 25.0,
        low_price_threshold: float = 0.05,
        low_price_max_cost: float = 25.0,
        micro_price_threshold: float = 0.2,
        micro_price_max_cost: float = 80.0,
        require_twitter_ok_for_entries: bool = True,
        hedge_enabled: bool = True,
        hedge_ratio: float = 0.35,
        max_hedges_per_primary: int = 1,
        hedge_min_true_edge: float = 0.02,
        fee_category: str = "mentions",
        fee_rate: float | None = None,
        slippage_rate: float = 0.005,
        spread_floor_multiplier: float = 0.4,
        short_hold_fee_window_s: float = 900.0,
        short_hold_extra_fee_rate: float = 0.01,
        uncertainty: float = 0.05,
        rate_prior_strength_h: float = 24.0,
        nb_phi_override: float | None = None,
        nb_phi_scale: float = 1.0,
        hawkes_alpha_scale: float = 1.0,
        hawkes_alpha: float | None = None,
        hawkes_beta_per_min: float = 0.18,
        log_path: str = "paper_trade_events.jsonl",
        picks_log_path: str = "paper_picks_snapshot.jsonl",
        log_max_lines: int = 400,
        log_max_bytes: int = 500_000,
        picks_overwrite_latest: bool = True,
        picks_max_lines: int = 60,
        picks_max_bytes: int = 300_000,
        stale_refresh_interval_s: float = 24.0,
        ws_down_force_refresh_s: float = 8.0,
    ) -> None:
        self.tick_s = tick_s
        self.bankroll = bankroll
        self.cash = bankroll
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_positions = max_positions
        self.max_event_exposure_pct = max_event_exposure_pct
        self.entry_edge = entry_edge
        self.exit_edge = exit_edge
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.stop_loss_dwell_s = max(0.0, float(stop_loss_dwell_s))
        self.entry_alignment_max_gap = max(0.0, float(entry_alignment_max_gap))
        self.position_size_disagreement_penalty = max(0.0, float(position_size_disagreement_penalty))
        self.cooldown_s = cooldown_s
        self.stop_loss_cooldown_s = stop_loss_cooldown_s
        self.count_change_cooldown_s = count_change_cooldown_s
        self.ended_count_change_cooldown_s = max(0.0, float(ended_count_change_cooldown_s))
        self.signal_flip_edge_buffer = max(0.0, float(signal_flip_edge_buffer))
        self.signal_flip_min_edge_gap = max(0.0, float(signal_flip_min_edge_gap))
        self.signal_flip_max_loss_pct = max(0.0, float(signal_flip_max_loss_pct))
        self.signal_flip_min_hold_s = max(0.0, float(signal_flip_min_hold_s))
        self.signal_flip_reentry_cooldown_s = max(0.0, float(signal_flip_reentry_cooldown_s))
        self.event_signal_flip_cooldown_s = max(0.0, float(event_signal_flip_cooldown_s))
        self.event_stop_loss_cooldown_s = max(0.0, float(event_stop_loss_cooldown_s))
        self.reentry_edge_buffer = max(0.0, float(reentry_edge_buffer))
        self.stop_loss_reentry_edge_buffer = max(0.0, float(stop_loss_reentry_edge_buffer))
        self.key_loss_streak_lock_count = max(1, int(key_loss_streak_lock_count))
        self.key_loss_lock_s = max(0.0, float(key_loss_lock_s))
        self.min_entry_price = min_entry_price
        self.min_trade_cost = max(1.0, float(min_trade_cost))
        self.low_price_threshold = low_price_threshold
        self.low_price_max_cost = low_price_max_cost
        self.micro_price_threshold = max(0.0, float(micro_price_threshold))
        self.micro_price_max_cost = max(1.0, float(micro_price_max_cost))
        self.require_twitter_ok_for_entries = bool(require_twitter_ok_for_entries)
        self.hedge_enabled = hedge_enabled
        self.hedge_ratio = hedge_ratio
        self.max_hedges_per_primary = max_hedges_per_primary
        self.hedge_min_true_edge = hedge_min_true_edge
        self.fee_model = FeeModel(category=fee_category, taker_fee_rate=fee_rate)
        self.fee_category = fee_category
        self.fee_rate = self.fee_model.rate
        self.slippage_rate = slippage_rate
        self.spread_floor_multiplier = max(0.0, float(spread_floor_multiplier))
        self.short_hold_fee_window_s = max(0.0, float(short_hold_fee_window_s))
        self.short_hold_extra_fee_rate = max(0.0, float(short_hold_extra_fee_rate))
        self.uncertainty = uncertainty
        self.rate_prior_strength_h = max(1e-6, float(rate_prior_strength_h))
        self.nb_phi_override = (
            max(1.001, float(nb_phi_override)) if nb_phi_override is not None else None
        )
        self.nb_phi_scale = max(0.01, float(nb_phi_scale))
        self.hawkes_alpha = max(0.0, float(hawkes_alpha)) if hawkes_alpha is not None else None
        self.hawkes_alpha_scale = (
            self.hawkes_alpha / max(DEFAULT_ALPHA_ORIG, 1e-9)
            if self.hawkes_alpha is not None
            else max(0.0, float(hawkes_alpha_scale))
        )
        self.hawkes_beta_per_min = max(1e-6, float(hawkes_beta_per_min))
        self.log_path = log_path
        self.picks_log_path = picks_log_path
        self.log_max_lines = max(1, int(log_max_lines))
        self.log_max_bytes = max(10_000, int(log_max_bytes))
        self.picks_overwrite_latest = bool(picks_overwrite_latest)
        self.picks_max_lines = max(1, int(picks_max_lines))
        self.picks_max_bytes = max(10_000, int(picks_max_bytes))
        self.stale_refresh_interval_s = max(2.0, float(stale_refresh_interval_s))
        self.ws_down_force_refresh_s = max(2.0, float(ws_down_force_refresh_s))
        self._last_trim_unix_by_path: dict[str, float] = {}
        self._last_stale_refresh_unix: float = 0.0
        self._prev_market_mids: dict[str, tuple[float, float]] = {}
        # Staleness guard: updated each tick so position sizing can scale down
        # when our tweet-count data hasn't been refreshed recently.
        self._last_count_age_s: float | None = None
        # Cross-market exposure cap: total cost across all brackets that share
        # the same underlying observable (e.g. "elon-musk-of-tweets-*") is
        # capped at this fraction of bankroll to avoid correlated-burst blowups.
        self.correlated_exposure_cap_pct: float = 0.60

        self.prices = PolymarketPrices()
        self.tweets = TweetStream()
        self.health = XTrackerHealth()
        self.model = PredictionModel(
            rate_prior_strength_h=self.rate_prior_strength_h,
            nb_phi_override=self.nb_phi_override,
            nb_phi_scale=self.nb_phi_scale,
            hawkes_alpha_scale=self.hawkes_alpha_scale,
            hawkes_beta_per_min=self.hawkes_beta_per_min,
        )

        self.positions: dict[str, dict[str, Any]] = {}
        self.realized_pnl = 0.0
        self.closed_count = 0
        self.trade_events: list[dict[str, Any]] = []
        self.last_closed_unix: dict[str, float] = {}
        self.last_closed_reason: dict[str, str] = {}
        self.last_stoploss_unix: dict[str, float] = {}
        self.last_event_signal_flip_unix: dict[str, float] = {}
        self.last_event_stoploss_unix: dict[str, float] = {}
        self.key_loss_streak: dict[str, int] = {}
        self.key_loss_lock_until_unix: dict[str, float] = {}
        self.event_count_anchor: dict[str, dict[str, Any]] = {}
        self.last_event_count: dict[str, int] = {}
        self.last_count_change_unix: dict[str, float] = {}
        self.last_error: str | None = None

        # Restore close/stop-loss memory from prior runs so cooldown guards
        # still apply after process restarts.
        self._hydrate_risk_memory_from_log()

    def _hydrate_risk_memory_from_log(self) -> None:
        if not self.log_path or not os.path.exists(self.log_path):
            return

        def _parse_unix(raw_ts: Any) -> float | None:
            dt = self._parse_iso(raw_ts)
            return dt.timestamp() if dt else None

        def _max_assign(dct: dict[str, float], key: str, val: float) -> None:
            cur = dct.get(key)
            if cur is None or val >= cur:
                dct[key] = val

        def _to_float(raw: Any) -> float | None:
            try:
                return float(raw)
            except Exception:
                return None

        try:
            with open(self.log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if str(row.get("action") or "").upper() != "SELL":
                        continue

                    event_slug = str(row.get("event_slug") or "")
                    label = str(row.get("label") or "")
                    if not event_slug or not label:
                        continue

                    ts_unix = _parse_unix(row.get("ts"))
                    if ts_unix is None:
                        continue

                    key = self._key(event_slug, label)
                    prev_closed = self.last_closed_unix.get(key)
                    _max_assign(self.last_closed_unix, key, ts_unix)
                    reason = str(row.get("reason") or "")
                    if reason and (prev_closed is None or ts_unix >= float(prev_closed)):
                        self.last_closed_reason[key] = reason
                    if reason == "stop_loss":
                        _max_assign(self.last_stoploss_unix, key, ts_unix)
                        _max_assign(self.last_event_stoploss_unix, event_slug, ts_unix)
                    elif reason == "signal_flip":
                        _max_assign(self.last_event_signal_flip_unix, event_slug, ts_unix)

                    pnl = _to_float(row.get("pnl"))
                    if pnl is None:
                        continue
                    if pnl < 0:
                        streak = int(self.key_loss_streak.get(key, 0)) + 1
                        self.key_loss_streak[key] = streak
                        if streak >= self.key_loss_streak_lock_count:
                            _max_assign(
                                self.key_loss_lock_until_unix,
                                key,
                                ts_unix + self.key_loss_lock_s,
                            )
                    else:
                        self.key_loss_streak[key] = 0
        except Exception:
            # Never fail startup because of log-history parsing.
            return

    def _sync_event_universe(self) -> None:
        changed = self.prices.refresh_event_universe()
        if changed:
            active_slugs = set(self.prices.event_slugs)
            self.event_count_anchor = {
                slug: val for slug, val in self.event_count_anchor.items() if slug in active_slugs
            }
            self.last_event_count = {
                slug: val for slug, val in self.last_event_count.items() if slug in active_slugs
            }
            self.last_count_change_unix = {
                slug: val for slug, val in self.last_count_change_unix.items() if slug in active_slugs
            }
            self.last_event_signal_flip_unix = {
                slug: val for slug, val in self.last_event_signal_flip_unix.items() if slug in active_slugs
            }
            self.last_event_stoploss_unix = {
                slug: val for slug, val in self.last_event_stoploss_unix.items() if slug in active_slugs
            }
            self.key_loss_streak = {
                key: val
                for key, val in self.key_loss_streak.items()
                if str(key).split("|", 1)[0] in active_slugs
            }
            self.key_loss_lock_until_unix = {
                key: val
                for key, val in self.key_loss_lock_until_unix.items()
                if str(key).split("|", 1)[0] in active_slugs
            }
        self.health.set_event_slugs(list(self.prices.event_slugs))

    @staticmethod
    def _key(event_slug: str, label: str) -> str:
        return f"{event_slug}|{label}"

    @staticmethod
    def _label_midpoint(label: str) -> float:
        txt = str(label).strip()
        if "-" in txt:
            a, b = txt.split("-", 1)
            try:
                return (float(a) + float(b)) / 2.0
            except Exception:
                return 0.0
        if txt.endswith("+"):
            try:
                return float(txt[:-1]) + 20.0
            except Exception:
                return 0.0
        return 0.0

    @staticmethod
    def _label_bounds(label: str) -> tuple[int, int] | None:
        txt = str(label).strip()
        m = re.match(r"^(\d+)-(\d+)$", txt)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.match(r"^(\d+)\+$", txt)
        if m:
            return int(m.group(1)), 9999
        return None

    @staticmethod
    def _parse_iso(raw: Any) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _entry_price(row: dict[str, Any], side: str) -> float | None:
        return row.get("yes_buy") if side == "YES" else row.get("no_buy")

    @staticmethod
    def _exit_price(row: dict[str, Any], side: str) -> float | None:
        return row.get("yes_sell") if side == "YES" else row.get("no_sell")

    @staticmethod
    def _mark_price(row: dict[str, Any], side: str) -> float | None:
        if side == "YES":
            b = row.get("yes_sell")
            a = row.get("yes_buy")
        else:
            b = row.get("no_sell")
            a = row.get("no_buy")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return (float(a) + float(b)) / 2.0
        return a if isinstance(a, (int, float)) else b if isinstance(b, (int, float)) else None

    def _short_hold_extra_fee(self, hold_s: float, notional: float) -> float:
        if self.short_hold_fee_window_s <= 0:
            return 0.0
        if hold_s >= self.short_hold_fee_window_s:
            return 0.0
        return max(0.0, float(notional) * self.short_hold_extra_fee_rate)

    def _fee_per_share(self, price: float) -> float:
        return self.fee_model.per_share(float(price), liquidity_role="taker")

    def _fee_usdc(self, shares: float, price: float) -> float:
        return self.fee_model.fee_usdc(float(shares), float(price), liquidity_role="taker")

    def _append_log(self, row: dict[str, Any]) -> None:
        with open(self.log_path, "a") as f:
            f.write(json.dumps(row) + "\n")
        self._maybe_trim_jsonl(self.log_path, self.log_max_lines, self.log_max_bytes)

    def _maybe_trim_jsonl(self, path: str, keep_lines: int, max_bytes: int) -> None:
        if keep_lines <= 0 or max_bytes <= 0:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size <= max_bytes:
            return

        now = time.time()
        last_trim = float(self._last_trim_unix_by_path.get(path, 0.0))
        if (now - last_trim) < 8.0:
            return

        data = _tail_lines_bytes(path, keep_lines=keep_lines)
        with open(path, "wb") as f:
            f.write(data)
        self._last_trim_unix_by_path[path] = now

    def _record_event(self, evt: dict[str, Any]) -> None:
        self.trade_events.append(evt)
        self.trade_events = self.trade_events[-18:]
        self._append_log(evt)

    def _market_velocity_penalty(
        self, pred: dict[str, Any], row: dict[str, Any]
    ) -> float:
        yes_buy = row.get("yes_buy")
        yes_sell = row.get("yes_sell")
        if not isinstance(yes_buy, (int, float)) or not isinstance(yes_sell, (int, float)):
            return 0.0
        mid = (float(yes_buy) + float(yes_sell)) / 2.0
        key = self._key(str(pred.get("event_slug")), str(pred.get("label")))
        now = time.time()
        prev = self._prev_market_mids.get(key)
        self._prev_market_mids[key] = (mid, now)
        if prev is None:
            return 0.0
        prev_mid, prev_ts = prev
        elapsed = now - prev_ts
        if elapsed < 0.5:
            return 0.0
        velocity = abs(mid - prev_mid) / (elapsed / 60.0)
        penalty = 0.0
        if velocity > 0.02:
            penalty = min(0.12, (velocity - 0.02) * 1.0)
        return penalty

    def _with_true_edges(
        self,
        pred: dict[str, Any],
        row: dict[str, Any],
    ) -> dict[str, Any]:
        out = dict(pred)

        model_p_yes = float(pred.get("model_p_yes") or 0.0)
        model_p_no = _clamp(1.0 - model_p_yes, 0.0, 1.0)

        yes_buy = row.get("yes_buy")
        yes_sell = row.get("yes_sell")
        no_buy = row.get("no_buy")
        no_sell = row.get("no_sell")

        spread_yes = 0.0
        if isinstance(yes_buy, (int, float)) and isinstance(yes_sell, (int, float)):
            spread_yes = max(0.0, float(yes_buy) - float(yes_sell))
        spread_no = 0.0
        if isinstance(no_buy, (int, float)) and isinstance(no_sell, (int, float)):
            spread_no = max(0.0, float(no_buy) - float(no_sell))

        # Conservative probability of winning: static uncertainty plus a
        # velocity penalty when the market is moving fast. Fast repricing means
        # someone else already saw information our data hasn't caught up with.
        velocity_penalty = self._market_velocity_penalty(pred, row)
        effective_uncertainty = self.uncertainty + velocity_penalty
        prob_profit_yes = _clamp(model_p_yes - effective_uncertainty, 0.0, 1.0)
        prob_profit_no = _clamp(model_p_no - effective_uncertainty, 0.0, 1.0)

        yes_entry_fee = self._fee_per_share(float(yes_buy)) if isinstance(yes_buy, (int, float)) else 0.0
        no_entry_fee = self._fee_per_share(float(no_buy)) if isinstance(no_buy, (int, float)) else 0.0

        if isinstance(yes_buy, (int, float)):
            true_edge_yes = (
                prob_profit_yes
                - float(yes_buy)
                - yes_entry_fee
                - self.slippage_rate
                - (0.5 * spread_yes)
            )
        else:
            true_edge_yes = -1.0

        if isinstance(no_buy, (int, float)):
            true_edge_no = (
                prob_profit_no
                - float(no_buy)
                - no_entry_fee
                - self.slippage_rate
                - (0.5 * spread_no)
            )
        else:
            true_edge_no = -1.0

        if true_edge_yes >= true_edge_no:
            pick_side = "YES"
            pick_true_edge = true_edge_yes
            prob_make_money = prob_profit_yes
            pick_spread = spread_yes
            pick_signal = "BUY_YES" if pick_true_edge >= self.entry_edge else "HOLD"
        else:
            pick_side = "NO"
            pick_true_edge = true_edge_no
            prob_make_money = prob_profit_no
            pick_spread = spread_no
            pick_signal = "BUY_NO" if pick_true_edge >= self.entry_edge else "HOLD"

        entry_threshold = self.entry_edge
        entry_block_reason = None

        # Spread-aware edge floor: an entry must clear the round-trip cost by a
        # safety margin. With fees+slip+spread, executing a $X position costs
        # ~2·(fee+slip+0.5·spread) round-trip — wider than entry_edge alone for
        # cheap, wide-spread brackets. Demand at least 1.4× round-trip cost on
        # top of the static entry_edge so we don't trade noise on illiquid books.
        entry_px_for_floor = yes_buy if pick_side == "YES" else no_buy
        entry_fee_for_floor = (
            self._fee_per_share(float(entry_px_for_floor))
            if isinstance(entry_px_for_floor, (int, float))
            else 0.0
        )
        # Conservatively approximate exit fee at the same price. Settlement
        # may avoid an exit trade, but paper mode assumes active risk-managed
        # taker exits unless a position is left to resolve.
        round_trip_cost = (2.0 * self.slippage_rate) + (2.0 * entry_fee_for_floor) + pick_spread
        spread_floor = self.entry_edge + self.spread_floor_multiplier * round_trip_cost
        if entry_threshold < spread_floor:
            entry_threshold = spread_floor
        if pick_signal in ("BUY_YES", "BUY_NO") and pick_true_edge < entry_threshold:
            pick_signal = "HOLD"
            entry_block_reason = "edge_below_spread_floor"

        # After event expiry, only allow:
        # - YES on the bracket containing current_count
        # - NO on all other brackets
        # This enables post-end mispricing captures while forbidding wrong-side bets.
        hours_left = pred.get("hours_left")
        ended_market = isinstance(hours_left, (int, float)) and float(hours_left) <= 0.0
        forced_end_mode = False
        if ended_market:
            current_count_raw = pred.get("current_count")
            bounds = self._label_bounds(str(pred.get("label") or row.get("label") or ""))
            if type(current_count_raw) is int and bounds is not None:
                low, high = bounds
                is_winning_bucket = low <= int(current_count_raw) <= high
                forced_end_mode = True
                entry_threshold = 0.0

                # At/after expiry with known count, this bracket outcome is binary.
                # Use deterministic probabilities (no uncertainty haircut) so we can
                # exploit late mispricings near settlement.
                prob_profit_yes = 1.0 if is_winning_bucket else 0.0
                prob_profit_no = 0.0 if is_winning_bucket else 1.0
                if isinstance(yes_buy, (int, float)):
                    yes_entry_fee = self._fee_per_share(float(yes_buy))
                    true_edge_yes = (
                        prob_profit_yes
                        - float(yes_buy)
                        - yes_entry_fee
                        - self.slippage_rate
                        - (0.5 * spread_yes)
                    )
                else:
                    true_edge_yes = -1.0
                if isinstance(no_buy, (int, float)):
                    no_entry_fee = self._fee_per_share(float(no_buy))
                    true_edge_no = (
                        prob_profit_no
                        - float(no_buy)
                        - no_entry_fee
                        - self.slippage_rate
                        - (0.5 * spread_no)
                    )
                else:
                    true_edge_no = -1.0

                if is_winning_bucket:
                    pick_side = "YES"
                    pick_true_edge = true_edge_yes
                    prob_make_money = prob_profit_yes
                    pick_signal = "BUY_YES" if pick_true_edge > entry_threshold else "HOLD"
                else:
                    pick_side = "NO"
                    pick_true_edge = true_edge_no
                    prob_make_money = prob_profit_no
                    pick_signal = "BUY_NO" if pick_true_edge > entry_threshold else "HOLD"
            else:
                pick_signal = "HOLD"
                entry_block_reason = "ended_count_unknown"

        entry_px = yes_buy if pick_side == "YES" else no_buy
        if isinstance(entry_px, (int, float)) and float(entry_px) < self.min_entry_price:
            pick_signal = "HOLD"
            entry_block_reason = "price_below_min_entry"

        # Market-disagreement guard. Huge gap between model and market mid is a
        # signal that either model is overconfident or market knows something we
        # don't. Block the worst cases outright; let the sizing layer downscale
        # the rest. Use mid as the reference, not buy/sell, so spread doesn't
        # penalize wide-but-symmetric books.
        if isinstance(yes_buy, (int, float)) and isinstance(yes_sell, (int, float)):
            market_mid_yes = (float(yes_buy) + float(yes_sell)) / 2.0
            disagreement = abs(float(model_p_yes) - market_mid_yes)
        else:
            market_mid_yes = None
            disagreement = 0.0
        if (
            pick_signal in ("BUY_YES", "BUY_NO")
            and self.entry_alignment_max_gap > 0
            and disagreement > self.entry_alignment_max_gap
        ):
            pick_signal = "HOLD"
            entry_block_reason = entry_block_reason or "disagreement_too_high"

        # Block fresh entries while the count is mid-jump — market may be panic-selling
        # ahead of the price catching up to the new bracket reality.
        slug = str(pred.get("event_slug"))
        last_chg = self.last_count_change_unix.get(slug)
        count_change_cooldown_s = (
            self.ended_count_change_cooldown_s if forced_end_mode else self.count_change_cooldown_s
        )
        if (
            pick_signal in ("BUY_YES", "BUY_NO")
            and last_chg
            and (time.time() - last_chg) < count_change_cooldown_s
        ):
            pick_signal = "HOLD"
            entry_block_reason = entry_block_reason or (
                "ended_count_settling" if forced_end_mode else "count_just_changed"
            )

        out.update(
            {
                "spread_yes": round(spread_yes, 6),
                "spread_no": round(spread_no, 6),
                "prob_profit_yes": round(prob_profit_yes, 6),
                "prob_profit_no": round(prob_profit_no, 6),
                "fee_per_share_yes": round(yes_entry_fee, 6),
                "fee_per_share_no": round(no_entry_fee, 6),
                "true_edge_yes": round(true_edge_yes, 6),
                "true_edge_no": round(true_edge_no, 6),
                "pick_side": pick_side,
                "pick_true_edge": round(pick_true_edge, 6),
                "prob_make_money": round(prob_make_money, 6),
                "pick_signal": pick_signal,
                "entry_threshold": round(float(entry_threshold), 6),
                "entry_block_reason": entry_block_reason,
                "disagreement": round(float(disagreement), 6),
                "market_mid_yes": (
                    round(float(market_mid_yes), 6) if market_mid_yes is not None else None
                ),
            }
        )
        return out

    def _write_picks_snapshot(self, preds: list[dict[str, Any]]) -> None:
        invested: dict[str, float] = {}
        for key, pos in self.positions.items():
            invested[key] = float(pos.get("cost") or 0.0)
        rows = []
        for p in preds:
            key = self._key(str(p.get("event_slug")), str(p.get("label")))
            rows.append(
                {
                    "event_slug": p.get("event_slug"),
                    "label": p.get("label"),
                    "pick_signal": p.get("pick_signal"),
                    "pick_side": p.get("pick_side"),
                    "true_edge": p.get("pick_true_edge"),
                    "prob_make_money": p.get("prob_make_money"),
                    "current_count": p.get("current_count"),
                    "invested_cost": round(invested.get(key, 0.0), 3),
                    "entry_block_reason": p.get("entry_block_reason"),
                }
            )
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "n_picks": len(rows),
            "picks": rows,
        }
        mode = "w" if self.picks_overwrite_latest else "a"
        with open(self.picks_log_path, mode) as f:
            f.write(json.dumps(payload) + "\n")
        if not self.picks_overwrite_latest:
            self._maybe_trim_jsonl(self.picks_log_path, self.picks_max_lines, self.picks_max_bytes)

    @staticmethod
    def _remaining_time_txt(end_dt: datetime | None, now_dt: datetime) -> str:
        if end_dt is None:
            return "n/a"
        sec = int((end_dt - now_dt).total_seconds())
        if sec <= 0:
            return "ended"
        days, rem = divmod(sec, 24 * 3600)
        hrs, rem = divmod(rem, 3600)
        mins = rem // 60
        if days > 0:
            return f"{days}d {hrs:02d}h"
        return f"{hrs:02d}h {mins:02d}m"

    def _event_count_rows(
        self,
        tweet_state: dict[str, Any],
        health_state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        events = health_state.get("events") or {}
        live_counts = tweet_state.get("event_live_counts") or {}
        now_dt = datetime.now(timezone.utc)

        # Preserve deterministic order from configured event list.
        for slug in list(self.prices.event_slugs):
            evt = events.get(slug) if isinstance(events, dict) else None
            end_dt = self._parse_iso(evt.get("end")) if isinstance(evt, dict) else None

            count_val: int | None = None
            if isinstance(live_counts.get(slug), int):
                count_val = int(live_counts.get(slug))
            elif isinstance(evt, dict) and isinstance(evt.get("count"), int):
                count_val = int(evt.get("count"))

            count_src = "xtracker"
            if isinstance(live_counts.get(slug), int):
                count_src = "xtracker+live"

            rows.append(
                {
                    "event_slug": slug,
                    "count": count_val,
                    "remaining": self._remaining_time_txt(end_dt, now_dt),
                    "source": count_src,
                }
            )
        return rows

    def _open_position(
        self,
        pred: dict[str, Any],
        row: dict[str, Any],
        now_unix: float,
        forced_budget: float | None = None,
        is_hedge: bool = False,
        hedge_parent_key: str | None = None,
    ) -> bool:
        side = str(pred.get("pick_side") or "YES")
        entry = self._entry_price(row, side)
        if not isinstance(entry, (int, float)):
            return False
        entry = float(entry)
        if entry <= 0.001 or entry >= 0.999:
            return False
        # Hard guard: skip ultra-cheap contracts that can gap by huge % in one tick.
        if entry < self.min_entry_price:
            return False

        event_slug = str(pred.get("event_slug"))
        key = self._key(event_slug, str(pred.get("label")))
        if key in self.positions:
            return False
        if len(self.positions) >= self.max_positions:
            return False
        lock_until = self.key_loss_lock_until_unix.get(key)
        if lock_until and now_unix < lock_until:
            pred["entry_block_reason"] = pred.get("entry_block_reason") or "loss_lock_active"
            return False

        last_closed = self.last_closed_unix.get(key)
        if last_closed and (now_unix - last_closed) < self.cooldown_s:
            pred["entry_block_reason"] = pred.get("entry_block_reason") or "key_cooldown"
            return False
        last_reason = str(self.last_closed_reason.get(key) or "")
        if (
            last_reason == "signal_flip"
            and last_closed
            and (now_unix - last_closed) < self.signal_flip_reentry_cooldown_s
        ):
            pred["entry_block_reason"] = pred.get("entry_block_reason") or "flip_reentry_cooldown"
            return False
        last_sl = self.last_stoploss_unix.get(key)
        if last_sl and (now_unix - last_sl) < self.stop_loss_cooldown_s:
            pred["entry_block_reason"] = pred.get("entry_block_reason") or "stoploss_reentry_cooldown"
            return False
        last_event_sl = self.last_event_stoploss_unix.get(event_slug)
        if last_event_sl and (now_unix - last_event_sl) < self.event_stop_loss_cooldown_s:
            pred["entry_block_reason"] = pred.get("entry_block_reason") or "event_stoploss_cooldown"
            return False
        last_event_flip = self.last_event_signal_flip_unix.get(event_slug)
        if last_event_flip and (now_unix - last_event_flip) < self.event_signal_flip_cooldown_s:
            pred["entry_block_reason"] = pred.get("entry_block_reason") or "event_flip_cooldown"
            return False

        true_edge = float(pred.get("pick_true_edge") or 0.0)
        entry_threshold = float(pred.get("entry_threshold", self.entry_edge) or 0.0)
        if true_edge < entry_threshold:
            pred["entry_block_reason"] = pred.get("entry_block_reason") or "edge_below_entry"
            return False
        if last_reason in ("signal_flip", "edge_decay"):
            reentry_floor = max(entry_threshold, self.entry_edge + self.reentry_edge_buffer)
            if true_edge < reentry_floor:
                pred["entry_block_reason"] = pred.get("entry_block_reason") or "reentry_edge_too_low"
                return False
        elif last_reason == "stop_loss":
            reentry_floor = max(entry_threshold, self.entry_edge + self.stop_loss_reentry_edge_buffer)
            if true_edge < reentry_floor:
                pred["entry_block_reason"] = pred.get("entry_block_reason") or "stoploss_reentry_edge_too_low"
                return False

        edge_mult = min(2.0, max(0.6, true_edge / max(self.entry_edge, 1e-9)))
        # Shrink size when our prediction is far from market consensus. Big
        # disagreements are either alpha or model error — Kelly-style we should
        # bet less when we can't tell which.
        disagreement = float(pred.get("disagreement") or 0.0)
        disagreement_scale = (
            1.0 / (1.0 + disagreement * self.position_size_disagreement_penalty)
            if not is_hedge else 1.0
        )
        budget = (
            min(self.cash, float(forced_budget))
            if forced_budget is not None
            else min(
                self.cash,
                self.bankroll * self.risk_per_trade_pct * edge_mult * disagreement_scale,
            )
        )
        if entry < self.micro_price_threshold:
            budget = min(budget, self.micro_price_max_cost)
        # Extra risk cap for low-price contracts that still pass min_entry_price.
        if entry < self.low_price_threshold:
            budget = min(budget, self.low_price_max_cost)
        # Staleness penalty: scale down when tweet-count data hasn't refreshed.
        # Data >90 s old loses 10 % per minute of staleness, floored at 40 %.
        if self._last_count_age_s is not None and self._last_count_age_s > 90.0:
            staleness_scale = max(0.4, 1.0 - (self._last_count_age_s - 90.0) / 300.0)
            budget *= staleness_scale

        # Event-level cap to avoid over-concentration.
        event_cap = self.bankroll * self.max_event_exposure_pct
        event_used = sum(
            float(p.get("cost") or 0.0)
            for p in self.positions.values()
            if str(p.get("event_slug")) == event_slug
        )
        budget = min(budget, max(0.0, event_cap - event_used))

        # Cross-market correlated-underlying cap: all "elon-musk-of-tweets-*"
        # brackets share the same underlying signal (tweet rate). A single burst
        # can move all of them simultaneously. Cap total exposure to this family
        # at correlated_exposure_cap_pct of bankroll to avoid the May-02 blowup
        # where a 10-tweet burst in 10 minutes caused correlated losses across
        # every active bracket simultaneously.
        slug_root = "-".join(event_slug.split("-")[:4])
        correlated_used = sum(
            float(p.get("cost") or 0.0)
            for p in self.positions.values()
            if "-".join(str(p.get("event_slug", "")).split("-")[:4]) == slug_root
        )
        correlated_cap = self.bankroll * self.correlated_exposure_cap_pct
        budget = min(budget, max(0.0, correlated_cap - correlated_used))

        if budget < self.min_trade_cost:
            return False

        entry_fee_per_share = self._fee_per_share(entry)
        shares = budget / max(entry + entry_fee_per_share, 1e-9)
        trade_value = shares * entry
        entry_fee = self._fee_usdc(shares, entry)
        total_cost = trade_value + entry_fee
        if total_cost > budget and total_cost > 0:
            scale = max(0.0, budget / total_cost)
            shares *= scale
            trade_value = shares * entry
            entry_fee = self._fee_usdc(shares, entry)
            total_cost = trade_value + entry_fee
        if total_cost < self.min_trade_cost:
            return False

        self.cash -= total_cost
        entry_count_raw = pred.get("current_count")
        entry_count = int(entry_count_raw) if isinstance(entry_count_raw, int) else None
        entry_lambda = pred.get("lambda_remaining")
        entry_hours_left = pred.get("hours_left")
        self.positions[key] = {
            "event_slug": pred.get("event_slug"),
            "label": pred.get("label"),
            "side": side,
            "entry_price": entry,
            "shares": shares,
            "trade_value": trade_value,
            "entry_fee": entry_fee,
            "cost": total_cost,
            "entry_raw_edge": float(pred.get("edge") or 0.0),
            "entry_true_edge": true_edge,
            "entry_prob_make_money": float(pred.get("prob_make_money") or 0.0),
            "entry_count": entry_count,
            "entry_lambda_remaining": float(entry_lambda) if isinstance(entry_lambda, (int, float)) else None,
            "entry_hours_left": float(entry_hours_left) if isinstance(entry_hours_left, (int, float)) else None,
            "is_hedge": bool(is_hedge),
            "hedge_parent_key": hedge_parent_key,
            "entry_ts": datetime.now(timezone.utc).isoformat(),
        }
        self._record_event(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "BUY",
                "event_slug": pred.get("event_slug"),
                "label": pred.get("label"),
                "side": side,
                "price": round(entry, 6),
                "shares": round(shares, 3),
                "trade_value": round(trade_value, 3),
                "entry_fee": round(entry_fee, 6),
                "cost": round(total_cost, 3),
                "raw_edge": round(float(pred.get("edge") or 0.0), 6),
                "true_edge": round(true_edge, 6),
                "prob_make_money": round(float(pred.get("prob_make_money") or 0.0), 6),
                "current_count": entry_count,
                "lambda_remaining": round(float(entry_lambda), 3) if isinstance(entry_lambda, (int, float)) else None,
                "hours_left": round(float(entry_hours_left), 3) if isinstance(entry_hours_left, (int, float)) else None,
                "model_p_yes": pred.get("model_p_yes"),
                "market_p_yes": pred.get("market_p_yes"),
                "is_hedge": bool(is_hedge),
                "hedge_parent_key": hedge_parent_key,
            }
        )
        return True

    def _close_position(
        self,
        key: str,
        pos: dict[str, Any],
        row: dict[str, Any],
        pred: dict[str, Any],
        reason: str,
        now_unix: float,
    ) -> None:
        side = str(pos.get("side"))
        exit_price = self._exit_price(row, side)
        if not isinstance(exit_price, (int, float)):
            return

        exit_price = float(exit_price)
        shares = float(pos.get("shares") or 0.0)
        proceeds = shares * exit_price
        cost = float(pos.get("cost") or 0.0)
        entry_ts = self._parse_iso(pos.get("entry_ts"))
        hold_s = (now_unix - entry_ts.timestamp()) if entry_ts else float("inf")
        extra_short_hold_fee = self._short_hold_extra_fee(hold_s, proceeds)
        exit_fee = self._fee_usdc(shares, exit_price)
        proceeds_net = proceeds - exit_fee - extra_short_hold_fee
        pnl = proceeds_net - cost

        self.cash += proceeds_net
        self.realized_pnl += pnl
        self.closed_count += 1
        self.last_closed_unix[key] = now_unix
        self.last_closed_reason[key] = reason
        if reason == "stop_loss":
            self.last_stoploss_unix[key] = now_unix
            event_slug = str(pos.get("event_slug") or "")
            if event_slug:
                self.last_event_stoploss_unix[event_slug] = now_unix
        if reason == "signal_flip":
            event_slug = str(pos.get("event_slug") or "")
            if event_slug:
                self.last_event_signal_flip_unix[event_slug] = now_unix

        if pnl < 0:
            streak = int(self.key_loss_streak.get(key, 0)) + 1
            self.key_loss_streak[key] = streak
            if streak >= self.key_loss_streak_lock_count:
                self.key_loss_lock_until_unix[key] = max(
                    float(self.key_loss_lock_until_unix.get(key, 0.0)),
                    now_unix + self.key_loss_lock_s,
                )
        else:
            self.key_loss_streak[key] = 0
        self.positions.pop(key, None)

        exit_count_raw = pred.get("current_count")
        exit_count = int(exit_count_raw) if isinstance(exit_count_raw, int) else None
        entry_count = pos.get("entry_count")
        count_delta = (
            exit_count - int(entry_count)
            if isinstance(exit_count, int) and isinstance(entry_count, int)
            else None
        )
        exit_lambda = pred.get("lambda_remaining")
        exit_hours_left = pred.get("hours_left")
        self._record_event(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "SELL",
                "event_slug": pos.get("event_slug"),
                "label": pos.get("label"),
                "side": side,
                "entry_price": round(float(pos.get("entry_price") or 0.0), 6),
                "exit_price": round(exit_price, 6),
                "shares": round(shares, 3),
                "proceeds": round(proceeds, 3),
                "exit_fee": round(exit_fee, 6),
                "proceeds_net": round(proceeds_net, 3),
                "extra_short_hold_fee": round(extra_short_hold_fee, 6),
                "hold_seconds": round(float(hold_s), 3) if hold_s != float("inf") else None,
                "pnl": round(pnl, 3),
                "pnl_pct": round((pnl / cost) * 100.0, 3) if cost > 0 else None,
                "raw_edge": round(float(pred.get("edge") or 0.0), 6),
                "true_edge": round(float(pred.get("pick_true_edge") or 0.0), 6),
                "prob_make_money": round(float(pred.get("prob_make_money") or 0.0), 6),
                "entry_count": entry_count,
                "current_count": exit_count,
                "count_delta": count_delta,
                "lambda_remaining": round(float(exit_lambda), 3) if isinstance(exit_lambda, (int, float)) else None,
                "hours_left": round(float(exit_hours_left), 3) if isinstance(exit_hours_left, (int, float)) else None,
                "model_p_yes": pred.get("model_p_yes"),
                "market_p_yes": pred.get("market_p_yes"),
                "reason": reason,
            }
        )

    def _already_hedged_for_parent(self, parent_key: str) -> bool:
        for pos in self.positions.values():
            if pos.get("is_hedge") and pos.get("hedge_parent_key") == parent_key:
                return True
        return False

    def _select_hedges_for_primary(
        self,
        primary: dict[str, Any],
        event_preds: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self.hedge_enabled:
            return []
        primary_key = self._key(str(primary.get("event_slug")), str(primary.get("label")))
        if self._already_hedged_for_parent(primary_key):
            return []

        primary_mid = self._label_midpoint(str(primary.get("label")))
        primary_side = str(primary.get("pick_side"))
        primary_label = str(primary.get("label"))

        scored: list[tuple[float, dict[str, Any]]] = []
        for cand in event_preds:
            if cand is primary:
                continue
            if str(cand.get("label")) == primary_label:
                continue
            if str(cand.get("pick_signal")) not in ("BUY_YES", "BUY_NO"):
                continue
            if float(cand.get("pick_true_edge") or 0.0) < self.hedge_min_true_edge:
                continue
            key = self._key(str(cand.get("event_slug")), str(cand.get("label")))
            if key in self.positions:
                continue

            cand_side = str(cand.get("pick_side"))
            cand_mid = self._label_midpoint(str(cand.get("label")))
            distance = abs(primary_mid - cand_mid)
            proximity = 1.0 / (1.0 + distance)
            side_bonus = 1.0 if cand_side != primary_side else 0.4
            p_money = float(cand.get("prob_make_money") or 0.0)
            tedge = float(cand.get("pick_true_edge") or 0.0)
            score = (0.55 * tedge) + (0.25 * p_money) + (0.15 * proximity) + (0.05 * side_bonus)
            scored.append((score, cand))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[: self.max_hedges_per_primary]]

    def _manage_positions(
        self,
        price_by_key: dict[str, dict[str, Any]],
        pred_by_key: dict[str, dict[str, Any]],
        now_unix: float,
    ) -> None:
        for key in list(self.positions.keys()):
            pos = self.positions.get(key)
            if not pos:
                continue
            row = price_by_key.get(key)
            pred = pred_by_key.get(key)
            if not row:
                continue
            has_valid_pred = isinstance(pred, dict) and (
                "pick_signal" in pred and "pick_true_edge" in pred
            )
            if pred is None:
                pred = {}

            side = str(pos.get("side"))
            mark = self._mark_price(row, side)
            if not isinstance(mark, (int, float)):
                continue

            entry = float(pos.get("entry_price") or 0.0)
            true_edge = float(pred.get("pick_true_edge") or 0.0)
            pnl_pct = ((float(mark) - entry) / entry) if entry > 0 else 0.0
            entry_ts = self._parse_iso(pos.get("entry_ts"))
            hold_s = (now_unix - entry_ts.timestamp()) if entry_ts else float("inf")

            pick_signal = str(pred.get("pick_signal") or "HOLD")
            aligned = has_valid_pred and (
                (pick_signal == "BUY_YES" and side == "YES")
                or (pick_signal == "BUY_NO" and side == "NO")
            )
            same_side_edge = (
                float(pred.get("true_edge_yes") or 0.0)
                if side == "YES"
                else float(pred.get("true_edge_no") or 0.0)
            )
            flip_side_edge = (
                float(pred.get("true_edge_no") or 0.0)
                if side == "YES"
                else float(pred.get("true_edge_yes") or 0.0)
            )
            flip_edge_gap = flip_side_edge - same_side_edge

            reason = None
            if pnl_pct >= self.take_profit_pct:
                reason = "take_profit"
                pos.pop("stop_loss_breach_since_unix", None)
            elif pnl_pct <= -self.stop_loss_pct:
                # Dwell-time gate: thin prediction markets routinely whipsaw
                # 10-20% on a single large order and revert within minutes.
                # Require sustained breach before realizing the loss.
                breach_since = pos.get("stop_loss_breach_since_unix")
                if breach_since is None:
                    pos["stop_loss_breach_since_unix"] = now_unix
                elif (now_unix - float(breach_since)) >= self.stop_loss_dwell_s:
                    reason = "stop_loss"
            elif (
                has_valid_pred
                and not aligned
                and true_edge <= self.exit_edge
                and hold_s >= self.signal_flip_min_hold_s
            ):
                pos.pop("stop_loss_breach_since_unix", None)
                reason = "edge_decay"
            elif (
                has_valid_pred
                and not aligned
                and true_edge > (self.entry_edge + self.signal_flip_edge_buffer)
                and flip_edge_gap >= self.signal_flip_min_edge_gap
                and pnl_pct >= -self.signal_flip_max_loss_pct
                and hold_s >= self.signal_flip_min_hold_s
            ):
                pos.pop("stop_loss_breach_since_unix", None)
                reason = "signal_flip"
            else:
                # Price recovered above the stop threshold — reset the dwell
                # timer so the next breach starts fresh.
                pos.pop("stop_loss_breach_since_unix", None)

            if reason:
                self._close_position(key, pos, row, pred, reason, now_unix)

    def _twitter_ok_for_entries(
        self,
        tweet_state: dict[str, Any],
        health_state: dict[str, Any],
    ) -> bool:
        if not self.require_twitter_ok_for_entries:
            return True
        count = tweet_state.get("lifetime_count")
        count_age = tweet_state.get("count_age_s")
        count_source = str(tweet_state.get("count_source") or "")
        sse_alive = bool(health_state.get("sse_alive"))
        return bool(
            isinstance(count, int)
            and count_source not in ("none", "seed", "sse_stream_anchor")
            and (
                sse_alive
                or (isinstance(count_age, (int, float)) and float(count_age) <= 120.0)
            )
        )

    def _try_entries(
        self,
        price_by_key: dict[str, dict[str, Any]],
        pred_by_key: dict[str, dict[str, Any]],
        now_unix: float,
    ) -> None:
        actionable = [
            p
            for p in pred_by_key.values()
            if p.get("pick_signal") in ("BUY_YES", "BUY_NO")
            and float(p.get("pick_true_edge") or 0.0)
            >= float(p.get("entry_threshold", self.entry_edge) or 0.0)
        ]
        actionable.sort(key=lambda x: float(x.get("pick_true_edge") or 0.0), reverse=True)

        by_event: dict[str, list[dict[str, Any]]] = {}
        for p in actionable:
            by_event.setdefault(str(p.get("event_slug")), []).append(p)

        for event_slug, event_preds in by_event.items():
            if len(self.positions) >= self.max_positions:
                break
            last_event_flip = self.last_event_signal_flip_unix.get(event_slug)
            if last_event_flip and (now_unix - last_event_flip) < self.event_signal_flip_cooldown_s:
                continue
            # Primary = strongest true edge in event
            primary = event_preds[0]
            primary_key = self._key(str(primary.get("event_slug")), str(primary.get("label")))
            primary_row = price_by_key.get(primary_key)
            opened_primary = False
            if primary_row and primary_key not in self.positions:
                opened_primary = self._open_position(primary, primary_row, now_unix)

            # If primary exists (new or existing), add hedge legs from same event.
            if self.hedge_enabled and (opened_primary or primary_key in self.positions):
                primary_cost = float(self.positions.get(primary_key, {}).get("cost") or 0.0)
                if primary_cost > 0:
                    hedges = self._select_hedges_for_primary(primary, event_preds)
                    for hedge in hedges:
                        if len(self.positions) >= self.max_positions:
                            break
                        hedge_key = self._key(str(hedge.get("event_slug")), str(hedge.get("label")))
                        hedge_row = price_by_key.get(hedge_key)
                        if not hedge_row:
                            continue
                        self._open_position(
                            hedge,
                            hedge_row,
                            now_unix,
                            forced_budget=primary_cost * self.hedge_ratio,
                            is_hedge=True,
                            hedge_parent_key=primary_key,
                        )

    def _unrealized(self, price_by_key: dict[str, dict[str, Any]]) -> float:
        total = 0.0
        for key, pos in self.positions.items():
            row = price_by_key.get(key)
            if not row:
                continue
            mark = self._mark_price(row, str(pos.get("side")))
            if not isinstance(mark, (int, float)):
                continue
            value = float(pos.get("shares") or 0.0) * float(mark)
            total += value - float(pos.get("cost") or 0.0)
        return total

    def _health_flags(
        self,
        price_rows: list[dict[str, Any]],
        tweet_state: dict[str, Any],
        health_state: dict[str, Any],
    ) -> dict[str, Any]:
        count = tweet_state.get("lifetime_count")
        count_age = tweet_state.get("count_age_s")
        count_source = str(tweet_state.get("count_source") or "")
        sse_alive = bool(health_state.get("sse_alive"))

        twitter_ok = bool(
            isinstance(count, int)
            and count_source not in ("none", "seed", "sse_stream_anchor")
            and (
                sse_alive
                or (isinstance(count_age, (int, float)) and float(count_age) <= 120.0)
            )
        )

        xtracker_ok = bool(health_state.get("xtracker_alive"))

        total = len(price_rows)
        priced = 0
        live = 0
        for r in price_rows:
            has_prices = all(
                isinstance(r.get(k), (int, float))
                for k in ("yes_buy", "yes_sell", "no_buy", "no_sell")
            )
            if has_prices:
                priced += 1
            yes_age = r.get("yes_age_s")
            no_age = r.get("no_age_s")
            if isinstance(yes_age, (int, float)) and isinstance(no_age, (int, float)):
                if float(yes_age) <= 12.0 and float(no_age) <= 12.0:
                    live += 1

        polymarket_ok = bool(self.prices.ws_ok() and total > 0 and priced == total)

        return {
            "twitter_ok": twitter_ok,
            "xtracker_ok": xtracker_ok,
            "polymarket_ok": polymarket_ok,
            "priced_brackets": priced,
            "live_brackets": live,
            "total_brackets": total,
        }

    def _build_event_live_counts(
        self,
        tweet_state: dict[str, Any],
        health_state: dict[str, Any],
    ) -> dict[str, int]:
        out: dict[str, int] = {}
        lifetime = tweet_state.get("lifetime_count")
        lifetime_int = int(lifetime) if isinstance(lifetime, int) else None
        count_source = str(tweet_state.get("count_source") or "")
        events = health_state.get("events") or {}
        now = datetime.now(timezone.utc)
        # Capture data age so _open_position can apply the staleness penalty.
        age = tweet_state.get("count_age_s")
        self._last_count_age_s = float(age) if isinstance(age, (int, float)) else None
        # Guard against pathological jumps (e.g., stale/seeded baselines).
        max_reasonable_delta = 2000

        for slug, evt in events.items():
            if not isinstance(evt, dict):
                continue
            if not evt.get("ok") or not isinstance(evt.get("count"), int):
                continue
            xcount = int(evt.get("count"))
            start = self._parse_iso(evt.get("start"))
            end = self._parse_iso(evt.get("end"))
            is_active = bool(start and end and start <= now <= end)

            anchor = self.event_count_anchor.get(slug)
            if (
                anchor is None
                or int(anchor.get("xtracker_count", -1)) != xcount
                or (anchor.get("lifetime_anchor") is None and lifetime_int is not None)
            ):
                # Note: we deliberately do NOT reset on count_source change.
                # Source flips (graphql→sse or back) during polling gaps were
                # zeroing the delta and hiding tweet bursts for up to 3 minutes.
                anchor = {
                    "xtracker_count": xcount,
                    "lifetime_anchor": lifetime_int,
                    "lifetime_source": count_source or None,
                    "updated_at": now.isoformat(),
                }
                self.event_count_anchor[slug] = anchor

            live_count = xcount
            if is_active and lifetime_int is not None and anchor.get("lifetime_anchor") is not None:
                delta = max(0, lifetime_int - int(anchor.get("lifetime_anchor")))
                if delta > max_reasonable_delta:
                    # Re-anchor on suspicious jump so per-event count stays sane.
                    anchor["lifetime_anchor"] = lifetime_int
                    anchor["lifetime_source"] = count_source or None
                    anchor["updated_at"] = now.isoformat()
                    delta = 0
                live_count = xcount + delta
            out[str(slug)] = int(live_count)

            prev = self.last_event_count.get(slug)
            if prev is not None and live_count > prev:
                self.last_count_change_unix[slug] = time.time()
            self.last_event_count[slug] = int(live_count)
        return out

    def _render(
        self,
        price_rows: list[dict[str, Any]],
        tweet_state: dict[str, Any],
        health_state: dict[str, Any],
        model_state: dict[str, Any],
        pred_by_key: dict[str, dict[str, Any]],
        price_by_key: dict[str, dict[str, Any]],
    ) -> None:
        latest_tweet = tweet_state.get("latest_tweet") or {}
        latest_text = str(latest_tweet.get("tweet_text") or "").replace("\n", " ")[:92]

        event_names = {
            str(r.get("event_slug") or "")
            for r in price_rows
            if str(r.get("event_slug") or "")
        }
        event_names.update(
            str(pos.get("event_slug") or "")
            for pos in self.positions.values()
            if str(pos.get("event_slug") or "")
        )
        event_names.update(
            str(p.get("event_slug") or "")
            for p in pred_by_key.values()
            if str(p.get("event_slug") or "")
        )
        event_w = max(5, max((len(s) for s in event_names), default=28))
        label_w = 7

        health_flags = self._health_flags(price_rows, tweet_state, health_state)

        unreal = self._unrealized(price_by_key)
        equity = self.cash + sum(float(p.get("cost") or 0.0) for p in self.positions.values()) + unreal
        total_pnl = equity - self.bankroll

        print("\033[H\033[J", end="")
        print("=" * 120)
        print(
            f"POLYBOT PAPER TRADE | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC "
            f"| credit_per_model=${self.bankroll:.0f}"
        )
        print("=" * 120)
        print("DATA STREAM HEALTH")
        print(f"{'SOURCE':<54} {'STATUS':<6}")
        print(f"{'Elon Musk Twitter stream':<54} {'YES' if health_flags['twitter_ok'] else 'NO':<6}")
        print(f"{'XTracker stream':<54} {'YES' if health_flags['xtracker_ok'] else 'NO':<6}")
        print(
            f"{'Polymarket prices (all brackets)':<54} "
            f"{'YES' if health_flags['polymarket_ok'] else 'NO':<6}"
        )
        print(
            f"Polymarket details: priced={health_flags['priced_brackets']}/{health_flags['total_brackets']} "
            f"live={health_flags['live_brackets']}/{health_flags['total_brackets']} ws_ok={self.prices.ws_ok()}"
        )
        print(
            f"Costs: fee_category={self.fee_category} taker_fee_rate={self.fee_rate:.3f} "
            f"slippage={self.slippage_rate:.3f} spread_floor_x={self.spread_floor_multiplier:.2f} "
            f"uncertainty={self.uncertainty:.3f} min_trade_cost={self.min_trade_cost:.1f} "
            f"quick_fee<{self.short_hold_fee_window_s:.0f}s={self.short_hold_extra_fee_rate:.3f} "
            f"count_cd={self.count_change_cooldown_s:.0f}s ended_cd={self.ended_count_change_cooldown_s:.0f}s "
            f"stale_refresh={self.stale_refresh_interval_s:.0f}s"
        )
        print(
            f"SELL RULES: take_profit>={self.take_profit_pct:.1%} | stop_loss<={-self.stop_loss_pct:.1%} "
            f"| edge_decay(true_edge<={self.exit_edge:.1%}) | signal_flip(gap>={self.signal_flip_min_edge_gap:.3f}, "
            f"max_loss={self.signal_flip_max_loss_pct:.1%})"
        )
        print(
            f"REENTRY GUARDS: key_cd={self.cooldown_s:.0f}s stoploss_cd={self.stop_loss_cooldown_s:.0f}s "
            f"flip_key_cd={self.signal_flip_reentry_cooldown_s:.0f}s flip_event_cd={self.event_signal_flip_cooldown_s:.0f}s "
            f"stoploss_event_cd={self.event_stop_loss_cooldown_s:.0f}s lock={self.key_loss_streak_lock_count}x/{self.key_loss_lock_s:.0f}s "
            f"min_hold={self.signal_flip_min_hold_s:.0f}s reentry_edge+={self.reentry_edge_buffer:.3f} "
            f"sl_reentry_edge+={self.stop_loss_reentry_edge_buffer:.3f}"
        )
        print("-" * 120)
        print(
            f"cash={self.cash:.2f} unreal={unreal:+.2f} realized={self.realized_pnl:+.2f} "
            f"equity={equity:.2f} total_pnl={total_pnl:+.2f} positions={len(self.positions)} closed={self.closed_count}"
        )
        print(
            f"tweet_count={tweet_state.get('lifetime_count')} source={tweet_state.get('count_source')} "
            f"age={tweet_state.get('count_age_s')}s sentiment={model_state.get('sentiment')} "
            f"density_h={model_state.get('tweet_density_per_hour')}"
        )
        print(
            f"entry_gate={'OPEN' if self._twitter_ok_for_entries(tweet_state, health_state) else 'BLOCKED_twitter_stream_off'}"
        )
        print(
            f"latest_tweet_time={_fmt_ts(latest_tweet.get('tweet_created_at') or latest_tweet.get('detected_at'))} "
            f"tweet_id={latest_tweet.get('tweet_id')}"
        )
        print(f"latest_tweet_text={latest_text or 'n/a'}")
        print("-" * 120)
        print("BET TWEET COUNTS (ALL EVENTS)")
        print(f"{'EVENT':<{event_w}} {'COUNT':>9} {'REMAINING':>12} {'SOURCE':>14}")
        print("-" * 120)
        for r in self._event_count_rows(tweet_state, health_state):
            count_txt = str(r.get("count")) if isinstance(r.get("count"), int) else "n/a"
            print(
                f"{str(r.get('event_slug')):<{event_w}} "
                f"{count_txt:>9} {str(r.get('remaining')):>12} {str(r.get('source')):>14}"
            )
        print("-" * 120)
        print("OPEN POSITIONS (all)")
        print(
            f"{'EVENT':<{event_w}} {'BRACKET':>{label_w}} SIDE HEDGE ENTRY   MARK  COST$  TRUE_E  P(MONEY) PICK_SIG CUR_CNT"
        )
        print("-" * 120)

        for key, pos in sorted(self.positions.items(), key=lambda kv: kv[1].get("entry_ts", "")):
            pred = pred_by_key.get(key, {})
            row = price_by_key.get(key, {})
            side = str(pos.get("side"))
            mark = self._mark_price(row, side)
            hedge_tag = "YES" if pos.get("is_hedge") else "NO"
            print(
                f"{str(pos.get('event_slug')):<{event_w}} {str(pos.get('label')):>{label_w}} {side:>4} {hedge_tag:>5} "
                f"{_fmt(pos.get('entry_price')):>6} {_fmt(mark):>6} "
                f"{_fmt(pos.get('cost'), 1):>6} "
                f"{_fmt(pred.get('pick_true_edge'), 4):>7} {_fmt(pred.get('prob_make_money'), 4):>9} "
                f"{str(pred.get('pick_signal') or 'n/a'):>8} "
                f"{str(pred.get('current_count', 'n/a')):>8}"
            )

        if self.trade_events:
            evt = self.trade_events[-1]
            if evt.get("action") == "BUY":
                msg = (
                    f"LAST_FILL BUY {evt.get('event_slug')} {evt.get('label')} {evt.get('side')} "
                    f"px={evt.get('price')} true={evt.get('true_edge')}"
                )
            else:
                msg = (
                    f"LAST_FILL SELL {evt.get('event_slug')} {evt.get('label')} {evt.get('side')} "
                    f"pnl={evt.get('pnl')} reason={evt.get('reason')}"
                )
            print("-" * 120)
            print(msg[:120])

    def run(self) -> None:
        self.prices.start()
        self.tweets.start()

        while True:
            t0 = time.time()
            try:
                self._sync_event_universe()
                now_unix = time.time()
                ws_ok_now = self.prices.ws_ok(max_silence_s=max(10.0, self.tick_s * 2.0))
                needs_scheduled_refresh = (
                    (now_unix - self._last_stale_refresh_unix) >= self.stale_refresh_interval_s
                )
                needs_ws_recovery_refresh = (
                    not ws_ok_now
                    and (now_unix - self._last_stale_refresh_unix) >= self.ws_down_force_refresh_s
                )
                if needs_scheduled_refresh or needs_ws_recovery_refresh:
                    self.prices.refresh_stale_tokens(force_all=not ws_ok_now)
                    self._last_stale_refresh_unix = time.time()
                price_rows = self.prices.snapshot()
                tweet_state = self.tweets.poll()
                health_state = self.health.poll(tweet_state)
                tweet_state["event_live_counts"] = self._build_event_live_counts(tweet_state, health_state)
                model_state = self.model.predict(price_rows, tweet_state, health_state)

                price_by_key = {
                    self._key(str(r.get("event_slug")), str(r.get("label"))): r for r in price_rows
                }

                enriched: list[dict[str, Any]] = []
                for base_pred in model_state.get("predictions") or []:
                    key = self._key(str(base_pred.get("event_slug")), str(base_pred.get("label")))
                    row = price_by_key.get(key)
                    if not row:
                        continue
                    enriched.append(self._with_true_edges(base_pred, row))
                self._write_picks_snapshot(enriched)

                pred_by_key = {
                    self._key(str(p.get("event_slug")), str(p.get("label"))): p
                    for p in enriched
                }

                now_unix = time.time()
                self._manage_positions(price_by_key, pred_by_key, now_unix)
                if not self._twitter_ok_for_entries(tweet_state, health_state):
                    for pred in pred_by_key.values():
                        if str(pred.get("pick_signal") or "") in ("BUY_YES", "BUY_NO"):
                            pred["pick_signal"] = "HOLD"
                            pred["entry_block_reason"] = (
                                pred.get("entry_block_reason") or "twitter_stream_off"
                            )
                self._try_entries(price_by_key, pred_by_key, now_unix)
                self._render(
                    price_rows,
                    tweet_state,
                    health_state,
                    model_state,
                    pred_by_key,
                    price_by_key,
                )
                self.last_error = None
            except KeyboardInterrupt:
                return
            except Exception as exc:
                # Keep terminal clean; retain error in memory in case needed later.
                self.last_error = f"{type(exc).__name__}: {exc}"

            elapsed = time.time() - t0
            time.sleep(max(0.2, self.tick_s - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper trade terminal runner")
    parser.add_argument("--tick-s", type=float, default=8.0)
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--risk-per-trade", type=float, default=0.08)
    parser.add_argument("--max-positions", type=int, default=8)
    parser.add_argument("--max-event-exposure-pct", type=float, default=0.45)
    parser.add_argument("--entry-edge", type=float, default=0.08)
    parser.add_argument("--exit-edge", type=float, default=0.015)
    parser.add_argument("--take-profit", type=float, default=0.22)
    parser.add_argument("--stop-loss", type=float, default=0.14)
    parser.add_argument("--cooldown-s", type=float, default=60.0)
    parser.add_argument("--stop-loss-cooldown-s", type=float, default=7200.0)
    parser.add_argument("--count-change-cooldown-s", type=float, default=45.0)
    parser.add_argument("--ended-count-change-cooldown-s", type=float, default=900.0)
    parser.add_argument("--signal-flip-edge-buffer", type=float, default=0.03)
    parser.add_argument("--signal-flip-min-edge-gap", type=float, default=0.06)
    parser.add_argument("--signal-flip-max-loss-pct", type=float, default=0.03)
    parser.add_argument("--signal-flip-min-hold-s", type=float, default=600.0)
    parser.add_argument("--signal-flip-reentry-cooldown-s", type=float, default=1800.0)
    parser.add_argument("--event-signal-flip-cooldown-s", type=float, default=900.0)
    parser.add_argument("--event-stop-loss-cooldown-s", type=float, default=3600.0)
    parser.add_argument("--reentry-edge-buffer", type=float, default=0.02)
    parser.add_argument("--stop-loss-reentry-edge-buffer", type=float, default=0.06)
    parser.add_argument("--key-loss-streak-lock-count", type=int, default=2)
    parser.add_argument("--key-loss-lock-s", type=float, default=21600.0)
    parser.add_argument("--min-entry-price", type=float, default=0.02)
    parser.add_argument("--min-trade-cost", type=float, default=25.0)
    parser.add_argument("--low-price-threshold", type=float, default=0.05)
    parser.add_argument("--low-price-max-cost", type=float, default=25.0)
    parser.add_argument("--micro-price-threshold", type=float, default=0.2)
    parser.add_argument("--micro-price-max-cost", type=float, default=80.0)
    parser.add_argument("--require-twitter-ok-for-entries", action="store_true", default=True)
    parser.add_argument(
        "--allow-entries-without-twitter",
        dest="require_twitter_ok_for_entries",
        action="store_false",
    )
    parser.add_argument("--hedge-enabled", action="store_true", default=True)
    parser.add_argument("--no-hedge", dest="hedge_enabled", action="store_false")
    parser.add_argument("--hedge-ratio", type=float, default=0.35)
    parser.add_argument("--max-hedges-per-primary", type=int, default=1)
    parser.add_argument("--hedge-min-true-edge", type=float, default=0.02)
    parser.add_argument("--fee-category", default="mentions")
    parser.add_argument(
        "--fee-rate",
        type=float,
        default=None,
        help="Override Polymarket taker fee rate; defaults to category table.",
    )
    parser.add_argument("--slippage-rate", type=float, default=0.005)
    parser.add_argument("--spread-floor-multiplier", type=float, default=0.4)
    parser.add_argument("--short-hold-fee-window-s", type=float, default=900.0)
    parser.add_argument("--short-hold-extra-fee-rate", type=float, default=0.01)
    parser.add_argument("--uncertainty", type=float, default=0.05)
    parser.add_argument("--rate-prior-strength-h", type=float, default=24.0)
    parser.add_argument("--nb-phi", dest="nb_phi_override", type=float, default=None)
    parser.add_argument("--nb-phi-scale", type=float, default=1.0)
    parser.add_argument(
        "--hawkes-alpha",
        dest="hawkes_alpha",
        type=float,
        default=None,
        help="Absolute Hawkes excitation alpha; defaults to calibrated live model alpha.",
    )
    parser.add_argument("--hawkes-alpha-scale", type=float, default=1.0)
    parser.add_argument("--hawkes-beta", dest="hawkes_beta_per_min", type=float, default=0.18)
    parser.add_argument("--log", default="paper_trade_events.jsonl")
    parser.add_argument("--picks-log", default="paper_picks_snapshot.jsonl")
    parser.add_argument("--log-max-lines", type=int, default=400)
    parser.add_argument("--log-max-bytes", type=int, default=500000)
    parser.add_argument("--picks-overwrite-latest", action="store_true", default=True)
    parser.add_argument("--picks-append", dest="picks_overwrite_latest", action="store_false")
    parser.add_argument("--picks-max-lines", type=int, default=60)
    parser.add_argument("--picks-max-bytes", type=int, default=300000)
    parser.add_argument("--stale-refresh-interval-s", type=float, default=24.0)
    parser.add_argument("--ws-down-force-refresh-s", type=float, default=8.0)
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    PaperTradeTerminal(
        tick_s=args.tick_s,
        bankroll=args.bankroll,
        risk_per_trade_pct=args.risk_per_trade,
        max_positions=args.max_positions,
        max_event_exposure_pct=args.max_event_exposure_pct,
        entry_edge=args.entry_edge,
        exit_edge=args.exit_edge,
        take_profit_pct=args.take_profit,
        stop_loss_pct=args.stop_loss,
        cooldown_s=args.cooldown_s,
        stop_loss_cooldown_s=args.stop_loss_cooldown_s,
        count_change_cooldown_s=args.count_change_cooldown_s,
        ended_count_change_cooldown_s=args.ended_count_change_cooldown_s,
        signal_flip_edge_buffer=args.signal_flip_edge_buffer,
        signal_flip_min_edge_gap=args.signal_flip_min_edge_gap,
        signal_flip_max_loss_pct=args.signal_flip_max_loss_pct,
        signal_flip_min_hold_s=args.signal_flip_min_hold_s,
        signal_flip_reentry_cooldown_s=args.signal_flip_reentry_cooldown_s,
        event_signal_flip_cooldown_s=args.event_signal_flip_cooldown_s,
        event_stop_loss_cooldown_s=args.event_stop_loss_cooldown_s,
        reentry_edge_buffer=args.reentry_edge_buffer,
        stop_loss_reentry_edge_buffer=args.stop_loss_reentry_edge_buffer,
        key_loss_streak_lock_count=args.key_loss_streak_lock_count,
        key_loss_lock_s=args.key_loss_lock_s,
        min_entry_price=args.min_entry_price,
        min_trade_cost=args.min_trade_cost,
        low_price_threshold=args.low_price_threshold,
        low_price_max_cost=args.low_price_max_cost,
        micro_price_threshold=args.micro_price_threshold,
        micro_price_max_cost=args.micro_price_max_cost,
        require_twitter_ok_for_entries=args.require_twitter_ok_for_entries,
        hedge_enabled=args.hedge_enabled,
        hedge_ratio=args.hedge_ratio,
        max_hedges_per_primary=args.max_hedges_per_primary,
        hedge_min_true_edge=args.hedge_min_true_edge,
        fee_category=args.fee_category,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
        spread_floor_multiplier=args.spread_floor_multiplier,
        short_hold_fee_window_s=args.short_hold_fee_window_s,
        short_hold_extra_fee_rate=args.short_hold_extra_fee_rate,
        uncertainty=args.uncertainty,
        rate_prior_strength_h=args.rate_prior_strength_h,
        nb_phi_override=args.nb_phi_override,
        nb_phi_scale=args.nb_phi_scale,
        hawkes_alpha_scale=args.hawkes_alpha_scale,
        hawkes_alpha=args.hawkes_alpha,
        hawkes_beta_per_min=args.hawkes_beta_per_min,
        log_path=args.log,
        picks_log_path=args.picks_log,
        log_max_lines=args.log_max_lines,
        log_max_bytes=args.log_max_bytes,
        picks_overwrite_latest=args.picks_overwrite_latest,
        picks_max_lines=args.picks_max_lines,
        picks_max_bytes=args.picks_max_bytes,
        stale_refresh_interval_s=args.stale_refresh_interval_s,
        ws_down_force_refresh_s=args.ws_down_force_refresh_s,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
