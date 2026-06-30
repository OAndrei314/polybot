"""Generate 100 competitor JSON configs for the 96h experiment.

The pool is designed to discover good post-patch model parameters. Patches
in effect:
  - phi clamped to empirical floor (model/predictor.py _phi_for_window)
  - hold-to-resolution edge_decay guard (terminal/paper_trade.py)
  - dead-bucket entry filters (NO+>72h, <15c+>24h, >65c)

Strategy:
  - 4 hand-crafted "anchor" configs spanning paradigms
  - 96 Latin Hypercube samples across the 12 most-important axes

Output: data/experiments/96h_pool/*.json   (one per competitor)
Each file: {"competitor_id": "...", "params": {...}}

Re-run produces identical output thanks to fixed RNG seed.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

OUT_DIR = Path("data/experiments/96h_pool")
SEED = 20260517_2  # v4: hold-to-resolution after backtest showed +$33K vs -$29K recorded
N = 200          # more samples now that we know what zone to search in

# Hand-crafted anchors — v4 (2026-05-17): hold-to-resolution strategy
# after backtest validated +$33K on the SAME entries that recorded run lost
# $29K on. Key insight: model entries are profitable; TP/SL/edge_decay
# convert asymmetric +$0.45/-$0.55 payoff into death-by-1000-cuts.
#
# Critical defaults for ALL anchors:
#   - take_profit = 0.95 (effectively disabled; only fires on near-perfect TP)
#   - stop_loss = 0.95 (effectively disabled; only fires on catastrophic loss)
#   - edge_decay disabled in code (terminal/paper_trade.py default)
#   - max_event_exposure_pct = 0.10-0.20 (cap single-bracket damage)
#   - risk_per_trade_pct ≤ 0.04 (top-10 from v2 ran 0.02-0.05)
ANCHORS: list[tuple[str, dict]] = [
    (
        "anchor_h2r_balanced",
        {
            "entry_edge": 0.12,
            "take_profit": 0.95,
            "stop_loss": 0.95,
            "nb_phi": 5.0,            # match observed VMR ~3-6 better than empirical
            "nb_phi_scale": 0.6,      # additional scale-down (audit: model over-disperses)
            "rate_prior_strength_h": 8.0,   # was 20-50; recent rate diverges, react faster
            "hawkes_alpha": 0.20,
            "uncertainty": 0.05,
            "risk_per_trade_pct": 0.025,
            "spread_floor_multiplier": 0.5,
            "signal_flip_min_edge_gap": 0.10,
            "signal_flip_max_loss_pct": 0.50,  # disable signal_flip too
            "max_event_exposure_pct": 0.15,    # CAP per-event damage
            "position_size_disagreement_penalty": 1.0,
            "max_positions": 12,
            "min_entry_price": 0.04,
        },
    ),
    (
        "anchor_h2r_aggressive",
        {
            "entry_edge": 0.10,
            "take_profit": 0.95,
            "stop_loss": 0.95,
            "nb_phi": 5.0,
            "nb_phi_scale": 0.5,
            "rate_prior_strength_h": 6.0,
            "hawkes_alpha": 0.25,
            "uncertainty": 0.04,
            "risk_per_trade_pct": 0.035,
            "spread_floor_multiplier": 0.4,
            "signal_flip_min_edge_gap": 0.10,
            "signal_flip_max_loss_pct": 0.50,
            "max_event_exposure_pct": 0.20,
            "position_size_disagreement_penalty": 0.9,
            "max_positions": 15,
            "min_entry_price": 0.04,
        },
    ),
    (
        "anchor_h2r_selective",
        {
            "entry_edge": 0.18,
            "take_profit": 0.95,
            "stop_loss": 0.95,
            "nb_phi": 8.0,
            "nb_phi_scale": 0.7,
            "rate_prior_strength_h": 12.0,
            "hawkes_alpha": 0.18,
            "uncertainty": 0.06,
            "risk_per_trade_pct": 0.020,
            "spread_floor_multiplier": 0.6,
            "signal_flip_min_edge_gap": 0.12,
            "signal_flip_max_loss_pct": 0.50,
            "max_event_exposure_pct": 0.10,
            "position_size_disagreement_penalty": 1.3,
            "max_positions": 8,
            "min_entry_price": 0.05,
        },
    ),
    (
        "anchor_h2r_ultraconservative",
        {
            "entry_edge": 0.20,
            "take_profit": 0.95,
            "stop_loss": 0.95,
            "nb_phi": 10.0,
            "nb_phi_scale": 0.8,
            "rate_prior_strength_h": 15.0,
            "hawkes_alpha": 0.15,
            "uncertainty": 0.07,
            "risk_per_trade_pct": 0.015,
            "spread_floor_multiplier": 0.7,
            "signal_flip_min_edge_gap": 0.12,
            "signal_flip_max_loss_pct": 0.50,
            "max_event_exposure_pct": 0.10,
            "position_size_disagreement_penalty": 1.5,
            "max_positions": 6,
            "min_entry_price": 0.05,
        },
    ),
]


# v4 LHS dimensions — most parameters fixed at hold-to-resolution defaults.
# We only vary the DECISION-related knobs (entry, edge, rate prior, dispersion)
# since the EXIT-related knobs are all set to "disabled" via take_profit=0.95
# and stop_loss=0.95.
LHS_DIMS: list[tuple[str, float, float, bool]] = [
    ("entry_edge",                          0.08,  0.22, False),
    ("nb_phi",                              4.0,   25.0, False),  # narrower; will be clamped
    ("nb_phi_scale",                        0.4,   1.0,  False),  # NEW: scale phi DOWN
    ("rate_prior_strength_h",               4.0,   24.0, False),  # CRITICAL: much lower than v3
    ("hawkes_alpha",                        0.10,  0.35, False),
    ("uncertainty",                         0.03,  0.08, False),
    ("risk_per_trade_pct",                  0.015, 0.040, False),
    ("spread_floor_multiplier",             0.2,   0.8,  False),
    ("max_event_exposure_pct",              0.08,  0.25, False),  # CRITICAL: per-event cap
    ("position_size_disagreement_penalty",  0.7,   1.5,  False),
    ("signal_flip_min_edge_gap",            0.08,  0.15, False),  # high so signal_flip rarely fires
]


# v4: hold-to-resolution defaults
FIXED = {
    "bankroll": 1000.0,
    "exit_edge": 0.015,
    "max_positions": 10,
    "key_stop_loss_cooldown_s": 7200.0,
    "event_stop_loss_cooldown_s": 3600.0,
    "signal_flip_max_loss_pct": 0.50,   # was 0.03; disable signal_flip
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
    "hawkes_alpha_scale": 1.0,
    "hawkes_beta": 0.18,
    "stop_loss_dwell_s": 600.0,
    "entry_alignment_max_gap": 0.55,
    # HOLD-TO-RESOLUTION: take_profit and stop_loss set so they almost never fire.
    # Validated by backtest: hold_to_resolution made +$33K vs recorded -$29K
    # on the SAME entries. TP/SL convert positive-EV setups into losers.
    "take_profit": 0.95,
    "stop_loss": 0.95,
}


def lhs_samples(rng: random.Random, n: int, dims: list[tuple[str, float, float, bool]]) -> list[dict[str, float]]:
    """Classic Latin Hypercube Sampling. Each dim split into n bins; one sample per bin;
    independent random permutation per dim."""
    out: list[dict[str, float]] = []
    columns: dict[str, list[float]] = {}
    for name, lo, hi, is_int in dims:
        bins = list(range(n))
        rng.shuffle(bins)
        vals = []
        for b in bins:
            # uniform within the bin
            u = (b + rng.random()) / n
            v = lo + u * (hi - lo)
            if is_int:
                v = int(round(v))
            vals.append(v)
        columns[name] = vals
    for i in range(n):
        out.append({name: columns[name][i] for name, _, _, _ in dims})
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # clear any previous pool
    for p in OUT_DIR.glob("*.json"):
        p.unlink()

    rng = random.Random(SEED)

    written = []

    # Anchors first
    for cid, params in ANCHORS:
        cfg = {**FIXED, **params, "competitor_id": cid}
        # max_positions if overridden in anchor
        cfg["max_positions"] = int(params.get("max_positions", cfg["max_positions"]))
        path = OUT_DIR / f"{cid}.json"
        path.write_text(json.dumps({"competitor_id": cid, "params": cfg}, indent=2))
        written.append(cid)

    n_lhs = N - len(ANCHORS)
    samples = lhs_samples(rng, n_lhs, LHS_DIMS)

    # Per LHS slot, also pick max_positions discretely
    max_positions_choices = [4, 6, 8, 10, 12]
    for i, sample in enumerate(samples):
        cid = f"lhs_{i:03d}"
        cfg = {**FIXED, **sample, "competitor_id": cid}
        cfg["max_positions"] = max_positions_choices[i % len(max_positions_choices)]
        # round floats to a few decimals for readability
        for k in list(cfg):
            if isinstance(cfg[k], float):
                cfg[k] = round(cfg[k], 5)
        path = OUT_DIR / f"{cid}.json"
        path.write_text(json.dumps({"competitor_id": cid, "params": cfg}, indent=2))
        written.append(cid)

    print(f"Wrote {len(written)} configs to {OUT_DIR}")
    print(f"  anchors: {len(ANCHORS)}")
    print(f"  lhs:     {n_lhs}")

    # Quick coverage summary
    print()
    print("Coverage check (min, median, max) across LHS samples:")
    import statistics
    for name, lo, hi, _ in LHS_DIMS:
        vs = [s[name] for s in samples]
        print(f"  {name:38s} range=[{lo:7.3f}, {hi:7.3f}]  actual=[{min(vs):7.3f}, {statistics.median(vs):7.3f}, {max(vs):7.3f}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
