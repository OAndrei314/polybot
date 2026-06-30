"""Generate the v7 inversion-test pool — matched-pairs A/B.

WHY THIS POOL EXISTS
--------------------
The Gate 2 calibration analysis on v4 mature showed that of 5 unique
directional bets that the bot held to resolution, 4 lost and 1 won (the
"7.6% resolution win rate" was 1-of-5 directional decisions correct,
replicated across many competitors). The predictor audit confirmed the
math is correct end-to-end: no sign error, no off-by-one, no bracket
misalignment. The model's directional thesis just lost 4 of 5 specific
bracket bets this week.

The inversion hypothesis: if the model's resolution-bound directional
view carries systematic anti-signal, flipping the side at entry should
win where the original lost. If it's just sample-of-5 noise on this
particular week, inversion should be ~50% (random direction either way).

DESIGN — matched pairs A/B
--------------------------
  - 40 base parameter sets (same v5 BASE_DIMS LHS sweep, fresh seed)
  - Each base produces TWO competitors:
      <base>_norm   - invert_direction = False (control)
      <base>_inv    - invert_direction = True (treatment)
  - All other params identical within a pair: same entry_edge, exposure,
    risk, etc. phi held at empirical mid (nb_phi_scale = 0.65) so phi is
    not a confound. take_profit = 0.95, stop_loss = 0.95 (v4 defaults,
    hold-to-resolution so the inversion gets tested at resolution).
  - 40 pairs * 2 = 80 configs total (lighter than v5/v6's 200 to keep
    GQL load reasonable with 4 live arenas).

Total: 80 configs.

Competitor IDs:
  - i{base:02d}_norm   (control)
  - i{base:02d}_inv    (treatment)

run_select.is_v7_run() detects this pool by the i## prefix.

The matched-pair design lets a paired test directly measure the
inversion effect: per base, did inverted P&L exceed normal P&L?
Pre-registration: data/experiments/v7_inversion_prereg.md.

Output: data/experiments/v7_pool/*.json
"""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

from research.generate_96h_pool import FIXED, LHS_DIMS, lhs_samples

OUT_DIR = Path("data/experiments/v7_pool")
SEED = 20260526  # v7 inversion
N_BASE = 40

BASE_DIMS = [d for d in LHS_DIMS if d[0] not in ("nb_phi", "nb_phi_scale")]
MAX_POSITIONS_CHOICES = [4, 6, 8, 10, 12]

# Phi held constant at the empirical mid-range so phi is not a confound here.
PHI_FIXED_SCALE = 0.65


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
        base["nb_phi"] = None
        base["nb_phi_scale"] = PHI_FIXED_SCALE
        base["dynamic_phi_enabled"] = False
        # v4-style hold to resolution (so the inversion test actually
        # reaches the resolution boundary that Gate 2 measured on).
        base["take_profit"] = 0.95
        base["stop_loss"] = 0.95
        base["hard_exit_hours_left_min"] = None

        for tag, invert in (("norm", False), ("inv", True)):
            cid = f"i{i:02d}_{tag}"
            params = {**base, "competitor_id": cid, "invert_direction": invert}
            _write(cid, params)
            written += 1

    print(f"Wrote {written} configs to {OUT_DIR}")
    print(f"  {N_BASE} base parameter sets x 2 (normal/inverted) = {written} configs")
    print(f"  phi held constant: nb_phi=null, nb_phi_scale={PHI_FIXED_SCALE}")
    print(f"  take_profit=0.95, stop_loss=0.95 (hold-to-resolution, like v4)")
    print(f"  pre-registration: data/experiments/v7_inversion_prereg.md")
    print()
    print("Base-knob coverage (min, median, max) across the LHS bases:")
    for name, lo, hi, _ in BASE_DIMS:
        vs = [s[name] for s in bases]
        print(f"  {name:38s} range=[{lo:7.3f}, {hi:7.3f}]  "
              f"actual=[{min(vs):7.3f}, {statistics.median(vs):7.3f}, {max(vs):7.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
