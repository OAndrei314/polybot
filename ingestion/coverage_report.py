from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class CoverageReport:
    rows: int
    start: pd.Timestamp
    end: pd.Timestamp
    year_counts: pd.DataFrame
    month_counts: pd.DataFrame
    source_counts: pd.DataFrame
    source_year_counts: pd.DataFrame
    retweet_rates: pd.DataFrame
    missing_months: pd.DataFrame
    largest_gaps: pd.DataFrame
    latest_tweet: dict[str, Any]


def _safe_ts(raw: str | None) -> pd.Timestamp | None:
    if not raw:
        return None
    ts = pd.to_datetime(raw, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts


def _load_df(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Dataset not found: {p}")
    df = pd.read_parquet(p)
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"]).copy()
    return df


def build_report(
    df: pd.DataFrame,
    start: str = "2009-01-01T00:00:00Z",
    end: str | None = None,
) -> CoverageReport:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"])

    start_ts = _safe_ts(start) or pd.Timestamp("2009-01-01T00:00:00Z")
    end_ts = _safe_ts(end) or pd.Timestamp.now(tz="UTC")
    scoped = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)].copy()
    scoped = scoped.sort_values("date")

    if scoped.empty:
        empty = pd.DataFrame()
        return CoverageReport(
            rows=0,
            start=start_ts,
            end=end_ts,
            year_counts=empty,
            month_counts=empty,
            source_counts=empty,
            source_year_counts=empty,
            retweet_rates=empty,
            missing_months=empty,
            largest_gaps=empty,
            latest_tweet={},
        )

    year_counts = (
        scoped.assign(year=scoped["date"].dt.year)
        .groupby("year", as_index=False)["tweet_id"]
        .count()
        .rename(columns={"tweet_id": "tweet_count"})
        .sort_values("year")
    )

    month_counts = (
        scoped.set_index("date")
        .resample("MS")
        .size()
        .rename("tweet_count")
        .reset_index()
    )
    month_counts["month"] = month_counts["date"].dt.strftime("%Y-%m")
    month_counts = month_counts[["month", "tweet_count"]]

    source_counts = (
        scoped.groupby("source", as_index=False)["tweet_id"]
        .count()
        .rename(columns={"tweet_id": "tweet_count"})
        .sort_values("tweet_count", ascending=False)
    )

    source_year_counts = (
        scoped.assign(year=scoped["date"].dt.year)
        .groupby(["year", "source"], as_index=False)["tweet_id"]
        .count()
        .rename(columns={"tweet_id": "tweet_count"})
        .sort_values(["year", "source"])
    )

    retweet_rates = (
        scoped.groupby("source", as_index=False)["is_retweet"]
        .mean()
        .rename(columns={"is_retweet": "retweet_ratio"})
        .sort_values("retweet_ratio", ascending=False)
    )

    full_months = pd.date_range(
        start=start_ts.normalize().replace(day=1),
        end=end_ts.normalize().replace(day=1),
        freq="MS",
        tz="UTC",
    )
    month_lookup = dict(zip(month_counts["month"], month_counts["tweet_count"]))
    missing_rows: list[dict[str, Any]] = []
    for dt in full_months:
        key = dt.strftime("%Y-%m")
        if int(month_lookup.get(key, 0)) == 0:
            missing_rows.append({"month": key, "tweet_count": 0})
    missing_months = pd.DataFrame(missing_rows)

    # Spot suspicious silent periods even when month is non-zero.
    s = scoped["date"].sort_values().reset_index(drop=True)
    gaps = s.diff().dropna()
    gap_df = pd.DataFrame(
        {
            "from_ts": s.shift(1).dropna().values,
            "to_ts": s.iloc[1:].values,
            "gap_days": gaps.dt.total_seconds() / 86400.0,
        }
    ).sort_values("gap_days", ascending=False)
    largest_gaps = gap_df.head(25).copy()
    largest_gaps["from_ts"] = pd.to_datetime(largest_gaps["from_ts"], utc=True)
    largest_gaps["to_ts"] = pd.to_datetime(largest_gaps["to_ts"], utc=True)

    latest = scoped.iloc[-1]
    latest_tweet = {
        "tweet_id": str(latest.get("tweet_id", "")),
        "date": latest["date"].isoformat(),
        "source": str(latest.get("source", "")),
        "is_retweet": bool(latest.get("is_retweet", False)),
        "text": str(latest.get("text", ""))[:240],
    }

    return CoverageReport(
        rows=int(len(scoped)),
        start=start_ts,
        end=end_ts,
        year_counts=year_counts,
        month_counts=month_counts,
        source_counts=source_counts,
        source_year_counts=source_year_counts,
        retweet_rates=retweet_rates,
        missing_months=missing_months,
        largest_gaps=largest_gaps,
        latest_tweet=latest_tweet,
    )


def save_report(report: CoverageReport, out_dir: str | Path) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    report.year_counts.to_csv(out / "year_counts.csv", index=False)
    report.month_counts.to_csv(out / "month_counts.csv", index=False)
    report.source_counts.to_csv(out / "source_counts.csv", index=False)
    report.source_year_counts.to_csv(out / "source_year_counts.csv", index=False)
    report.retweet_rates.to_csv(out / "retweet_rates.csv", index=False)
    report.missing_months.to_csv(out / "missing_months.csv", index=False)
    report.largest_gaps.to_csv(out / "largest_gaps.csv", index=False)

    summary = {
        "rows": report.rows,
        "start": report.start.isoformat(),
        "end": report.end.isoformat(),
        "latest_tweet": report.latest_tweet,
        "missing_months": int(len(report.missing_months)),
    }
    with open(out / "summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)


def print_report(report: CoverageReport) -> None:
    print("TWEET COVERAGE REPORT")
    print("-" * 80)
    print(f"rows={report.rows:,}")
    print(f"window={report.start.isoformat()} -> {report.end.isoformat()}")
    print(f"missing_months={len(report.missing_months)}")
    if report.latest_tweet:
        lt = report.latest_tweet
        print(
            "latest_tweet="
            f"{lt['date']} id={lt['tweet_id']} source={lt['source']} "
            f"is_rt={lt['is_retweet']}"
        )
        print(f"latest_text={lt['text']}")

    print("\nTop sources:")
    if report.source_counts.empty:
        print("  n/a")
    else:
        for row in report.source_counts.itertuples(index=False):
            print(f"  {row.source:<20} {int(row.tweet_count):>8,}")

    print("\nYear counts:")
    if report.year_counts.empty:
        print("  n/a")
    else:
        for row in report.year_counts.itertuples(index=False):
            print(f"  {int(row.year)}: {int(row.tweet_count):>7,}")

    if not report.missing_months.empty:
        print("\nMissing months (first 12):")
        for row in report.missing_months.head(12).itertuples(index=False):
            print(f"  {row.month}")

    if not report.largest_gaps.empty:
        print("\nLargest inactivity gaps (days):")
        for row in report.largest_gaps.head(10).itertuples(index=False):
            print(f"  {row.gap_days:6.2f}  {row.from_ts} -> {row.to_ts}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Coverage/gap report for merged tweet history")
    parser.add_argument("--cache", default="data/tweets_all.parquet")
    parser.add_argument("--start", default="2009-01-01T00:00:00Z")
    parser.add_argument("--end", default=None, help="UTC ISO timestamp (default: now)")
    parser.add_argument("--out-dir", default="data/reports/coverage")
    args = parser.parse_args()

    df = _load_df(args.cache)
    report = build_report(df, start=args.start, end=args.end)
    print_report(report)
    save_report(report, args.out_dir)
    print(f"\nSaved CSV/JSON outputs to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
