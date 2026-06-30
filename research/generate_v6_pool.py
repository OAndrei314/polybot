"""Generate the v6 competitor pool: a TP/SL ladder testing vol-capture.

WHY THIS POOL EXISTS
--------------------
The Gate 2 calibration analysis on v4 mature data revealed:
  - take_profit exits: 715 trades, 94% win rate (the strategy's actual edge)
  - resolution exits:  355 trades, 7.6% win rate (the model's directional
    thesis is wrong at the resolution boundary)
  - stop_loss exits:   228 trades, 0% win rate (catastrophe-only)

The v4/v5 strategy makes money by capturing rare +95% price moves before
the (mostly-losing) bracket resolution arrives. v6 tests whether tightening
the exit thresholds — capturing smaller moves more often — produces a
better $/risk profile, and whether forcing exit BEFORE resolution beats
riding the resolution-bound losers.

DESIGN
------
  - 40 base parameter sets (same v5 BASE_DIMS LHS sweep, different seed)
  - Bases 00-19: HARD-EXIT — hard_exit_hours_left_min = 8.0
                 (force exit when <8h to bracket resolution, never let
                 resolution branch fire)
  - Bases 20-39: HYBRID    — hard_exit_hours_left_min = None
                 (no hard exit, resolution still allowed as a fallback)
  - Each base replicated at 5 TP/SL rungs (TP == SL, symmetric):
                 [0.10, 0.15, 0.20, 0.25, 0.30]
  - phi is held at the v5 LIVE empirical setting (nb_phi=null, scale=0.65)
    so phi is NOT the swept variable here.

Total: 40 bases x 5 rungs = 200 configs.

Competitor IDs: c{base:02d}_{kind}_tp{int(tp*100):03d}
  - kind = "h" for hard-exit (bases 0-19), "y" for hybrid (bases 20-39)
  - e.g., c07_h_tp020, c23_y_tp025

run_select.is_v6_run() detects this pool by the c## prefix.

Output: data/experiments/v6_pool/*.json
"""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

from research.generate_96h_pool import FIXED, LHS_DIMS, lhs_samples

OUT_DIR = Path("data/experiments/v6_pool")
SEED = 20260524  # v6 vol-capture
N_BASE = 40
N_HARD = 20  # bases 0..19 get hard exit; bases 20..39 are hybrid
TP_SL_RUNGS: list[float] = [0.10, 0.15, 0.20, 0.25, 0.30]

# Same base-dim sweep as v5: every v4 decision knob EXCEPT phi knobs and the
# exit knobs we're sweeping in v6 (take_profit, stop_loss).
BASE_DIMS = [d for d in LHS_DIMS if d[0] not in ("nb_phi", "nb_phi_scale")]
MAX_POSITIONS_CHOICES = [4, 6, 8, 10, 12]

# Phi held constant at the empirical mid-range so phi is not a confound here.
# (The phi-ladder experiment is testing phi separately in v5.)
PHI_FIXED_SCALE = 0.65

# Hard-exit threshold (hours of bracket window remaining when we force close).
HARD_EXIT_HOURS = 8.0


def _write(cid: str, params: dict) -> None:
    (OUT_DIR / f"{cid}.json").write_text(
        json.dumps({"competitor_id": cid, "params": params}, indent=2)
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for p in OUT_DIR.glob("*.json"):
        p.unlink()

    rng = random.Random(SEED)
    bases = lhs_samples(rng, N_BASE, BASE_DIMS)

    written = 0
    for i, sample in enumerate(bases):
        base = {**FIXED, **sample}
        base["max_positions"] = MAX_POSITIONS_CHOICES[i % len(MAX_POSITIONS_CHOICES)]
        for k in list(base):
            if isinstance(base[k], float):
                base[k] = round(base[k], 5)
        # Override v4-FIXED defaults that this experiment is sweeping/overriding.
        base["nb_phi"] = None
        base["nb_phi_scale"] = PHI_FIXED_SCALE
        base["dynamic_phi_enabled"] = False

        is_hard = i < N_HARD
        kind = "h" if is_hard else "y"
        base["hard_exit_hours_left_min"] = HARD_EXIT_HOURS if is_hard else None

        for tp in TP_SL_RUNGS:
            cid = f"c{i:02d}_{kind}_tp{int(round(tp * 100)):03d}"
            params = {**base,
                      "competitor_id": cid,
                      "take_profit": tp,
                      "stop_loss": tp}
            _write(cid, params)
            written += 1

    print(f"Wrote {written} configs to {OUT_DIR}")
    print(f"  {N_BASE} base parameter sets x {len(TP_SL_RUNGS)} TP/SL rungs")
    print(f"  bases 00-{N_HARD - 1:02d}: HARD-EXIT (hard_exit_hours_left_min = {HARD_EXIT_HOURS}h)")
    print(f"  bases {N_HARD:02d}-{N_BASE - 1:02d}: HYBRID (resolution still allowed)")
    print(f"  TP/SL rungs: {TP_SL_RUNGS}")
    print(f"  phi held constant: nb_phi=null, nb_phi_scale={PHI_FIXED_SCALE}")
    print()
    print("Base-knob coverage (min, median, max) across the LHS bases:")
    for name, lo, hi, _ in BASE_DIMS:
        vs = [s[name] for s in bases]
        print(f"  {name:38s} range=[{lo:7.3f}, {hi:7.3f}]  "
              f"actual=[{min(vs):7.3f}, {statistics.median(vs):7.3f}, {max(vs):7.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
