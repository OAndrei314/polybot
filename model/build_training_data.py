from __future__ import annotations

import argparse
from pathlib import Path

from ingestion.features import build_feature_dfs
from ingestion.merger import load_merged


def build_and_save_features(
    refresh: bool = False,
    out_dir: str | Path = "data/features",
) -> tuple[Path, Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    raw = load_merged(refresh=refresh)
    per_tweet, daily, weekly = build_feature_dfs(raw)

    per_tweet_path = out / "per_tweet.parquet"
    daily_path = out / "daily.parquet"
    weekly_path = out / "weekly.parquet"

    per_tweet.to_parquet(per_tweet_path, index=False)
    daily.to_parquet(daily_path, index=False)
    weekly.to_parquet(weekly_path, index=False)

    print(f"Saved per_tweet: {len(per_tweet):,} rows -> {per_tweet_path}")
    print(f"Saved daily:     {len(daily):,} rows -> {daily_path}")
    print(f"Saved weekly:    {len(weekly):,} rows -> {weekly_path}")
    return per_tweet_path, daily_path, weekly_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build model-ready feature tables from merged tweet history")
    parser.add_argument("--refresh", action="store_true", help="Rebuild merged tweets from raw sources first")
    parser.add_argument("--out-dir", default="data/features")
    args = parser.parse_args()

    build_and_save_features(refresh=args.refresh, out_dir=args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
