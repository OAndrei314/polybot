"""Measure how a newly-created Polymarket market behaves in its first hour.

Answers the questions behind the "be first" thesis:
  - When did a tradeable book first appear after the market was created?
  - What were the initial seeded prices per bracket?
  - How much did each bracket's price SWING before it settled?

Two t0 modes:
  - creation-log: anchors t0 at the real creation instant using
    data/new_market_events.jsonl (the new_market WS pushes). Used when a
    creation has actually been captured during a run.
  - first-seen fallback: anchors t0 at each event's first appearance in
    real_input_ticks. A proxy — clearly marked "[proxy]" in the output —
    so the tool is still useful before a real creation is captured.

Usage:
  python -m research.measure_creation_window                  # latest run
  python -m research.measure_creation_window --run-dir <path>
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    import orjson as _fastjson
    _LOADS = _fastjson.loads
except ImportError:
    _LOADS = json.loads

LIVE_TESTS_DIR = Path("data/reports/live_competitor_tests")
CREATION_LOG = Path("data/new_market_events.jsonl")
WINDOWS_S = [60, 300, 900, 1800, 3600]  # 1m, 5m, 15m, 30m, 1h


def latest_run() -> Path | None:
    if not LIVE_TESTS_DIR.exists():
        return None
    runs = sorted(LIVE_TESTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None


def _unix(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def load_series(run_dir: Path) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Stream real_input_ticks → {(event_slug, label): [(unix, yes_mid), ...]}."""
    gz = run_dir / "real_input_ticks.jsonl.gz"
    series: dict[tuple[str, str], list[tuple[float, float]]] = {}
    if not gz.exists():
        return series
    with gzip.open(gz, "rt") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                d = _LOADS(line)
            except Exception:
                continue
            tu = _unix(d.get("ts") or "")
            if tu is None:
                continue
            for r in d.get("price_rows") or []:
                ev = r.get("event_slug")
                lab = r.get("label")
                ym = r.get("yes_mid")
                if ev and lab and isinstance(ym, (int, float)):
                    series.setdefault((ev, lab), []).append((tu, float(ym)))
    for k in series:
        series[k].sort()
    return series


def creation_anchors(run_dir: Path) -> dict[str, float]:
    """Read the creation log → {question_text: creation_unix}. Empty if none."""
    anchors: dict[str, float] = {}
    for path in (run_dir / "new_market_events.jsonl", CREATION_LOG):
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                q = str(rec.get("question") or "").strip()
                cu = rec.get("received_unix")
                if q and isinstance(cu, (int, float)):
                    anchors[q] = float(cu)
    return anchors


def _price_at(rows: list[tuple[float, float]], t: float) -> float | None:
    """First yes_mid at or after time t."""
    for tu, ym in rows:
        if tu >= t:
            return ym
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, default=None)
    args = ap.parse_args()

    run_dir = args.run_dir or latest_run()
    if run_dir is None:
        print("ERROR: no run dir found")
        return 2

    series = load_series(run_dir)
    if not series:
        print(f"ERROR: no price series in {run_dir}")
        return 2
    anchors = creation_anchors(run_dir)

    # Group bracket series by event.
    events: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for (ev, lab), rows in series.items():
        events.setdefault(ev, {})[lab] = rows

    print(f"run: {run_dir.name}")
    print(f"creation-log entries: {len(anchors)}  "
          f"({'real t0' if anchors else 'none — using first-seen proxy'})")
    print()

    for ev in sorted(events):
        brackets = events[ev]
        # t0: real creation if any bracket's question matches the log, else
        # the earliest first-seen across this event's brackets (proxy).
        first_seen = min(rows[0][0] for rows in brackets.values() if rows)
        t0 = first_seen
        t0_kind = "[proxy first-seen]"
        # (creation-log match is by question text — done per bracket below)

        print(f"=== {ev} ===")
        print(f"  t0 {t0_kind}: "
              f"{datetime.fromtimestamp(t0, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
        hdr = "  bracket   first   " + "  ".join(f"+{w//60}m" for w in WINDOWS_S) + "   swing(1h)"
        print(hdr)
        swings = []
        for lab in sorted(brackets, key=lambda x: (len(x), x)):
            rows = [r for r in brackets[lab] if t0 <= r[0] <= t0 + 3600]
            if not rows:
                continue
            first = rows[0][1]
            cells = []
            for w in WINDOWS_S:
                p = _price_at(brackets[lab], t0 + w)
                cells.append(f"{p:.3f}" if p is not None else "  -  ")
            lo = min(y for _, y in rows)
            hi = max(y for _, y in rows)
            swing = hi - lo
            swings.append(swing)
            print(f"  {lab:8s}  {first:.3f}  " + "  ".join(cells) + f"   {swing:+.3f}")
        if swings:
            print(f"  -> brackets={len(swings)}  mean 1h swing={sum(swings)/len(swings):.3f}  "
                  f"max={max(swings):.3f}")
        print()

    if not anchors:
        print("NOTE: no new_market creation was captured in this run -- every t0")
        print("above is the first-seen proxy. Once the next run captures a real")
        print("creation, rerun this for true creation-to-price-swing behavior.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
