"""Generate the v5 competitor pool: a controlled phi LADDER.

v4 cohort analysis and the independent Gate-2 calibration check both pointed
at the same suspect: the dispersion phi may be too WIDE, making the model
under-confident. That is a hypothesis, not a result. The honest way to test
it is not to pin "the perfect narrow phi" (that overfits to one resolved
week) — it is to sweep phi as a controlled experiment and let a
pre-registered test say whether narrower phi actually helps.

DESIGN — a matched phi ladder
-----------------------------
  - N_BASE base parameter sets, Latin-Hypercube sampled over every v4
    decision knob EXCEPT the two phi knobs (nb_phi, nb_phi_scale).
  - Each base is replicated at PHI_LEVELS distinct phi widths. The ONLY
    thing that changes across a base's replicas is nb_phi_scale.
  - phi width is set via nb_phi_scale (a multiplier on the empirical phi
    curve); nb_phi is left None so there is no hard dispersion override.
  - dynamic phi is OFF for the whole pool — this experiment isolates the
    STATIC phi level. (The dynamic-phi A/B already ran and showed no
    effect; mixing it back in would just add noise.)

This gives N_BASE matched groups of PHI_LEVELS configs each. Within a
group every knob is identical except phi, so the group is a clean
controlled comparison. Across groups the bases differ, which is what lets
a paired test (narrowest vs widest, one pair per base) factor the
base-parameter noise out — see data/experiments/v5_phi_prereg.md.

Competitor IDs: b<base>_phi<level>, e.g. b07_phi035 is base 7 at
nb_phi_scale 0.35. The b##/phi### encoding lets analyze_phi_ladder.py
recover the (base, level) grid from filenames alone.

Output: data/experiments/v5_pool/*.json   (N_BASE * len(PHI_LEVELS) files)
Re-run is deterministic (fixed seed).
"""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

from research.generate_96h_pool import FIXED, LHS_DIMS, lhs_samples

OUT_DIR = Path("data/experiments/v5_pool")
SEED = 20260522  # v5 phi-ladder

# The swept ladder. nb_phi_scale multiplies the empirical phi curve, so
# 0.35 = much narrower than calibration (more confident model), 1.00 =
# calibration as-is. The rungs span the v4 LHS nb_phi_scale range so the
# ladder stays inside parameter regions v4 actually explored.
PHI_LEVELS: list[float] = [0.35, 0.50, 0.65, 0.80, 1.00]

# Number of base parameter sets. N_BASE * len(PHI_LEVELS) configs total.
N_BASE = 40

# Base LHS dimensions: every v4 decision knob EXCEPT the phi knobs, which
# the ladder controls explicitly.
BASE_DIMS = [d for d in LHS_DIMS if d[0] not in ("nb_phi", "nb_phi_scale")]

# max_positions is a per-base property — it must stay constant across a
# base's phi replicas, or it would confound the comparison.
MAX_POSITIONS_CHOICES = [4, 6, 8, 10, 12]


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
        # The base parameter set, shared by all PHI_LEVELS replicas.
        base = {**FIXED, **sample}
        base["max_positions"] = MAX_POSITIONS_CHOICES[i % len(MAX_POSITIONS_CHOICES)]
        for k in list(base):
            if isinstance(base[k], float):
                base[k] = round(base[k], 5)

        for level in PHI_LEVELS:
            cid = f"b{i:02d}_phi{int(round(level * 100)):03d}"
            params = {
                **base,
                "competitor_id": cid,
                # The ladder rung: scale the empirical phi, no hard override.
                "nb_phi": None,
                "nb_phi_scale": level,
                # Static-phi experiment — dynamic phi stays off pool-wide.
                "dynamic_phi_enabled": False,
            }
            _write(cid, params)
            written += 1

    print(f"Wrote {written} configs to {OUT_DIR}")
    print(f"  {N_BASE} base parameter sets x {len(PHI_LEVELS)} phi rungs")
    print(f"  phi rungs (nb_phi_scale): {PHI_LEVELS}")
    print(f"  nb_phi=None (no override), dynamic_phi_enabled=False pool-wide")
    print()
    print("Base-knob coverage (min, median, max) across the LHS bases:")
    for name, lo, hi, _ in BASE_DIMS:
        vs = [s[name] for s in bases]
        print(f"  {name:38s} range=[{lo:7.3f}, {hi:7.3f}]  "
              f"actual=[{min(vs):7.3f}, {statistics.median(vs):7.3f}, {max(vs):7.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
