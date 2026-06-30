"""Analyze the v5 phi-ladder run — the PRE-REGISTERED test only.

This script runs exactly the analysis locked in data/experiments/v5_phi_prereg.md
before the run started. It does not search for a better statistic or a better
phi level after the fact; doing so is how one week of data lies to you.

Pre-registered analysis:
  PRIMARY  - Wilcoxon signed-rank, two-sided, on the 40 paired differences
             d_i = PnL(base i, phi=0.35) - PnL(base i, phi=1.00). p < 0.05.
  EFFECT   - median paired difference (USD) + matched-pairs rank-biserial r.
  SECONDARY- Friedman test across the 5 rungs; Page's trend test for the
             ordered alternative "P&L decreasing in nb_phi_scale".
  DESCRIPT - per-rung mean/median P&L with bootstrap 95% CIs, count of bases
             where the narrowest rung beat the widest, per-rung trade counts.

Outputs:
  <out-dir>/phi_ladder.png            - P&L-vs-phi plot
  <run-dir>/phi_ladder_report.md      - the written report
  stdout                              - the same report

Usage:
  python -m research.analyze_phi_ladder                     # latest v5 replay run
  python -m research.analyze_phi_ladder --run-dir <path>
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
from pathlib import Path

import numpy as np
from scipy import stats

from research.run_select import latest_run

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# The locked ladder. nb_phi_scale rungs, ascending = narrower -> wider.
PHI_LEVELS = [0.35, 0.50, 0.65, 0.80, 1.00]
NARROW, WIDE = 0.35, 1.00
_ID_RE = re.compile(r"^b(\d+)_phi(\d+)$")
GREEN, RED, GREY = "#16a34a", "#dc2626", "#94a3b8"


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_pnl(run_dir: Path) -> tuple[dict[str, float], str]:
    """Per-competitor final P&L. Prefer the orphan-corrected leaderboard."""
    corrected = run_dir / "leaderboard_corrected.csv"
    latest = run_dir / "leaderboard_latest.csv"
    if corrected.exists():
        rows = _read_csv(corrected)
        return ({r["competitor_id"]: float(r["corrected_total_pnl"]) for r in rows},
                "leaderboard_corrected.csv")
    if latest.exists():
        rows = _read_csv(latest)
        return ({r["competitor_id"]: float(r["total_pnl"]) for r in rows},
                "leaderboard_latest.csv (FALLBACK - corrected leaderboard absent)")
    raise SystemExit(f"no leaderboard CSV found under {run_dir}")


def load_trade_counts(run_dir: Path) -> dict[str, int]:
    """Per-competitor buy count, for the phi-binds diagnostic. latest only."""
    latest = run_dir / "leaderboard_latest.csv"
    if not latest.exists():
        return {}
    counts: dict[str, int] = {}
    for r in _read_csv(latest):
        try:
            counts[r["competitor_id"]] = int(float(r.get("buys") or 0))
        except (ValueError, TypeError):
            counts[r["competitor_id"]] = 0
    return counts


def build_grid(pnl: dict[str, float]) -> dict[int, dict[float, float]]:
    """competitor_id b<base>_phi<level> -> grid[base][phi_level] = pnl."""
    grid: dict[int, dict[float, float]] = {}
    for cid, val in pnl.items():
        m = _ID_RE.match(cid)
        if not m:
            continue
        base = int(m.group(1))
        level = int(m.group(2)) / 100.0
        grid.setdefault(base, {})[level] = val
    return grid


def boot_ci(vals: list[float], func, n: int = 10000, seed: int = 20260522) -> tuple[float, float]:
    arr = np.asarray(vals, dtype=float)
    if len(arr) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(n, len(arr)))
    samples = func(arr[idx], axis=1)
    return (float(np.percentile(samples, 2.5)), float(np.percentile(samples, 97.5)))


def rank_biserial(diffs: list[float]) -> tuple[float, float, float]:
    """Matched-pairs rank-biserial r from signed ranks. Returns (r, W+, W-)."""
    nz = [d for d in diffs if d != 0.0]
    if not nz:
        return (0.0, 0.0, 0.0)
    order = sorted(range(len(nz)), key=lambda i: abs(nz[i]))
    ranks = [0.0] * len(nz)
    i = 0
    while i < len(nz):
        j = i
        while j + 1 < len(nz) and abs(nz[order[j + 1]]) == abs(nz[order[i]]):
            j += 1
        avg = (i + j) / 2.0 + 1.0  # average rank for the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_pos = sum(r for d, r in zip(nz, ranks) if d > 0)
    w_neg = sum(r for d, r in zip(nz, ranks) if d < 0)
    total = w_pos + w_neg
    r = (w_pos - w_neg) / total if total else 0.0
    return (r, w_pos, w_neg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--base-dir", default="data/reports/live_competitor_tests")
    ap.add_argument("--out-dir", default="notebooks/png/v5")
    args = ap.parse_args()

    run_dir = Path(args.run_dir) if args.run_dir else latest_run(Path(args.base_dir), "v5")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pnl, pnl_source = load_pnl(run_dir)
    trades = load_trade_counts(run_dir)
    grid = build_grid(pnl)

    # Keep only bases with the full 5-rung ladder.
    complete = {b: d for b, d in grid.items() if all(l in d for l in PHI_LEVELS)}
    L: list[str] = []  # report lines

    def out(line: str = "") -> None:
        L.append(line)

    out(f"# v5 phi-ladder analysis")
    out()
    out(f"- run: `{run_dir.name}`")
    out(f"- P&L source: {pnl_source}")
    out(f"- pre-registration: `data/experiments/v5_phi_prereg.md`")
    out(f"- bases with a complete 5-rung ladder: {len(complete)} / {len(grid)} parsed")
    out()

    if len(complete) < 6:
        out("**Not enough complete bases to run the pre-registered test.**")
        out("Expected `b##_phi###` competitor IDs from `generate_v5_pool.py`.")
        out("This run was probably not a phi-ladder pool.")
        report = "\n".join(L)
        print(report)
        (run_dir / "phi_ladder_report.md").write_text(report, encoding="utf-8")
        return 1

    bases = sorted(complete)
    # Matrix: rows = bases, cols = PHI_LEVELS (ascending).
    mat = np.array([[complete[b][l] for l in PHI_LEVELS] for b in bases])

    # ---- PRIMARY: Wilcoxon signed-rank, narrowest vs widest -----------------
    narrow = mat[:, PHI_LEVELS.index(NARROW)]
    wide = mat[:, PHI_LEVELS.index(WIDE)]
    diffs = (narrow - wide).tolist()
    med_d = statistics.median(diffs)
    r_rb, w_pos, w_neg = rank_biserial(diffs)
    n_narrow_wins = sum(1 for d in diffs if d > 0)
    n_ties = sum(1 for d in diffs if d == 0)

    if all(d == 0 for d in diffs):
        w_stat, w_p = float("nan"), 1.0
        primary_note = "all paired diffs are 0 - phi did not change P&L at all"
    else:
        w_res = stats.wilcoxon(narrow, wide, zero_method="wilcox",
                               alternative="two-sided")
        w_stat, w_p = float(w_res.statistic), float(w_res.pvalue)
        primary_note = ""

    out("## PRIMARY - Wilcoxon signed-rank (two-sided)")
    out()
    out(f"H0: median of d_i = PnL(phi={NARROW}) - PnL(phi={WIDE}) is 0.")
    out()
    out(f"- n bases (paired): {len(bases)}  (ties dropped by test: {n_ties})")
    out(f"- median paired diff: **${med_d:+,.2f}**  "
        f"(narrower minus wider; positive => narrower helped)")
    out(f"- bases where narrower beat wider: {n_narrow_wins} / {len(bases)}")
    out(f"- Wilcoxon W = {w_stat:.1f}   "
        f"sum(+ranks) = {w_pos:.1f}   sum(-ranks) = {w_neg:.1f}")
    out(f"- matched-pairs rank-biserial r = **{r_rb:+.3f}**")
    out(f"- **p = {w_p:.4f}**")
    if primary_note:
        out(f"- note: {primary_note}")
    out()
    # Direction read from the sign of rank-biserial r, NOT the median: with
    # many tied diffs (within-base phi spreads of $0.00 are common when phi
    # doesn't change a competitor's trades) the median sits at 0 even when
    # the unbroken-tie ranks are lopsided. r captures that.
    if w_p < 0.05 and r_rb > 0:
        verdict = ("REJECT H0. Narrower phi produced higher P&L on this replay "
                   f"(rank-biserial r={r_rb:+.3f}, median diff ${med_d:+,.2f}). "
                   "Per the prereg decision rule: promote a narrow-phi config to "
                   "the next LIVE (non-replay) week as a candidate - NOT to real "
                   "money, and NOT yet validated (single-week replay).")
    elif w_p < 0.05 and r_rb < 0:
        verdict = ("REJECT H0, but in the OPPOSITE direction - wider phi helped "
                   f"(rank-biserial r={r_rb:+.3f}, median diff ${med_d:+,.2f}). "
                   "The under-confidence hypothesis is wrong as stated. Per the "
                   "prereg decision rule: stop narrowing phi; re-examine the "
                   "calibration analysis. Read the descriptive table below - if "
                   "the absolute rung-mean gap is tiny relative to per-competitor "
                   "P&L, the 'effect' is real but economically trivial.")
    else:
        verdict = ("FAIL TO REJECT H0. No detectable phi effect on this replay. "
                   "Per the prereg decision rule: do not change phi from the v4 "
                   "calibration default. Check the trade-count table below - if "
                   "rungs traded near-identically, phi did not bind (a power "
                   "problem), which is different from 'no phi effect'.")
    out(f"**Verdict: {verdict}**")
    out()

    # ---- SECONDARY: Friedman + Page's trend test ----------------------------
    out("## SECONDARY - trend across all 5 rungs")
    out()
    try:
        fr = stats.friedmanchisquare(*[mat[:, i] for i in range(len(PHI_LEVELS))])
        out(f"- Friedman chi2 = {fr.statistic:.3f}, p = {fr.pvalue:.4f}  "
            f"(any rung effect at all?)")
    except Exception as exc:
        out(f"- Friedman test failed: {exc}")
    # Page's test: predicted order is INCREASING P&L as phi narrows, so feed
    # the rungs widest -> narrowest and let the default increasing alternative
    # encode H1 (P&L decreasing in nb_phi_scale).
    try:
        page_mat = mat[:, ::-1]  # columns now phi 1.00 -> 0.35
        pg = stats.page_trend_test(page_mat)
        out(f"- Page's L = {pg.statistic:.1f}, p = {pg.pvalue:.4f}  "
            f"(ordered alternative: P&L monotonically decreasing in nb_phi_scale)")
    except Exception as exc:
        out(f"- Page's trend test failed: {exc}")
    out()

    # ---- DESCRIPTIVE --------------------------------------------------------
    out("## DESCRIPTIVE - per-rung summary (no inference)")
    out()
    out("| nb_phi_scale | mean P&L | mean 95% CI | median P&L | mean buys | zero-trade bases |")
    out("|---|---|---|---|---|---|")
    rung_means: list[float] = []
    rung_ci: list[tuple[float, float]] = []
    for i, level in enumerate(PHI_LEVELS):
        col = mat[:, i].tolist()
        mean_v = statistics.mean(col)
        med_v = statistics.median(col)
        lo, hi = boot_ci(col, np.mean)
        rung_means.append(mean_v)
        rung_ci.append((lo, hi))
        tcounts = [trades.get(f"b{b:02d}_phi{int(round(level*100)):03d}", 0)
                   for b in bases]
        mean_buys = statistics.mean(tcounts) if tcounts else 0.0
        zero_trade = sum(1 for t in tcounts if t == 0)
        out(f"| {level:.2f} | ${mean_v:+,.2f} | [${lo:+,.0f}, ${hi:+,.0f}] | "
            f"${med_v:+,.2f} | {mean_buys:.1f} | {zero_trade}/{len(bases)} |")
    out()

    # phi-binds diagnostic
    spread = max(rung_means) - min(rung_means)
    all_buys = [trades.get(f"b{b:02d}_phi{int(round(l*100)):03d}", 0)
                for b in bases for l in PHI_LEVELS]
    if all_buys and statistics.mean(all_buys) < 1.0:
        out("> **Caveat:** competitors barely traded - the phi lever could not "
            "express itself. A null result here is a power problem, not "
            "evidence that phi has no effect.")
        out()
    elif spread < 1.0:
        out("> **Caveat:** rung means are within $1 of each other - phi did not "
            "bind on this run (entry/exposure gates dominated). Treat a null "
            "result as 'phi did not bind', not 'phi has no effect'.")
        out()

    # ---- PLOT ---------------------------------------------------------------
    fig, ax = plt.subplots(figsize=(9, 6))
    for r_idx in range(mat.shape[0]):
        row = mat[r_idx]
        c = GREEN if (row[0] - row[-1]) > 0 else RED
        ax.plot(PHI_LEVELS, row, color=c, alpha=0.18, lw=0.9, zorder=1)
    yerr = np.array([[m - lo for m, (lo, _) in zip(rung_means, rung_ci)],
                     [hi - m for m, (_, hi) in zip(rung_means, rung_ci)]])
    ax.errorbar(PHI_LEVELS, rung_means, yerr=yerr, color="#1d4ed8", lw=2.6,
                marker="o", ms=7, capsize=5, zorder=3, label="rung mean (95% CI)")
    ax.axhline(0, color=GREY, lw=1, ls="--", zorder=0)
    ax.set_xlabel("nb_phi_scale  (0.35 = narrow / confident  ->  1.00 = calibration)")
    ax.set_ylabel("final total P&L (USD)")
    ax.set_xticks(PHI_LEVELS)
    sig = f"p={w_p:.4f}" if not (w_p != w_p) else "p=n/a"
    ax.set_title(f"v5 phi-ladder - {run_dir.name}\n"
                 f"primary (narrow vs wide) Wilcoxon {sig}, "
                 f"median diff ${med_d:+,.0f}", fontsize=11)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=9)
    fig.tight_layout()
    plot_path = out_dir / "phi_ladder.png"
    fig.savefig(plot_path, dpi=160)
    plt.close(fig)
    out(f"plot written: `{plot_path}`")

    report = "\n".join(L)
    print(report)
    (run_dir / "phi_ladder_report.md").write_text(report, encoding="utf-8")
    print(f"\nreport written: {run_dir / 'phi_ladder_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
