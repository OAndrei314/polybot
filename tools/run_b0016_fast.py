from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from terminal.paper_trade import PaperTradeTerminal


def main() -> int:
    bot = PaperTradeTerminal(
        tick_s=1.0,
        bankroll=1000.0,
        risk_per_trade_pct=0.03839849310890061,
        max_positions=8,
        max_event_exposure_pct=0.39010117003969214,
        entry_edge=0.06403893452308901,
        exit_edge=0.005026827980206602,
        take_profit_pct=0.32598901336446623,
        stop_loss_pct=0.316354878588745,
        stop_loss_dwell_s=272.2467463055546,
        entry_alignment_max_gap=0.4676679067549555,
        position_size_disagreement_penalty=0.7907425446413674,
        stop_loss_cooldown_s=626.5202412687273,
        event_stop_loss_cooldown_s=5326.078648020797,
        signal_flip_min_edge_gap=0.014586369753791684,
        signal_flip_max_loss_pct=0.02612279709224758,
        key_loss_streak_lock_count=4,
        key_loss_lock_s=2475.169343732325,
        min_entry_price=0.1,
        min_trade_cost=25.0,
        low_price_threshold=0.05,
        low_price_max_cost=25.0,
        micro_price_threshold=0.2,
        micro_price_max_cost=79.9216369201531,
        require_twitter_ok_for_entries=True,
        hedge_enabled=False,
        fee_category="mentions",
        fee_rate=None,
        slippage_rate=0.005,
        spread_floor_multiplier=1.1794351543060126,
        uncertainty=0.03777801933807922,
        rate_prior_strength_h=17.279192425796698,
        nb_phi_override=12.702047454959065,
        nb_phi_scale=1.0,
        hawkes_alpha=0.22338124504987084,
        hawkes_alpha_scale=1.0,
        hawkes_beta_per_min=0.04150502525421535,
        log_path="logs/b_0016_fast_paper_trades.jsonl",
        picks_log_path="logs/b_0016_fast_picks_latest.jsonl",
        log_max_lines=2_000_000,
        log_max_bytes=500_000_000,
        picks_overwrite_latest=True,
        stale_refresh_interval_s=8.0,
        ws_down_force_refresh_s=4.0,
    )
    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
