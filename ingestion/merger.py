"""
Merge all tweet sources into a single deduplicated DataFrame.

Priority (highest wins on duplicate tweet_id):
  1. x_api_full_archive — official X full-archive export cache (if available)
  2. xtracker_api       — live API feed
  3. local_archive:*    — local JSON exports (e.g. downloaded 2010–2025 bundles)
  4. gist_archive_2010_2025 — external archive (covers 2010–2025)
  5. sse_signal_log     — real-time browser detections
  6. twscrape           — scraped historical (recent timeline)
  7. kaggle:*           — static archives

Merged result is cached to data/tweets_all.parquet for fast reloads.
Call load_merged(refresh=True) to force a full rebuild.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from ingestion.sources import (
    BASE_COLS,
    load_gist_archive,
    load_local_archive_data,
    load_x_api_full_archive_data,
    load_kaggle_datasets,
    load_sse_rows,
    load_twscrape_data,
    load_xtracker_posts,
    standardize_frame,
)

_CACHE_PATH = Path("data/tweets_all.parquet")

_SOURCE_PRIORITY: dict[str, int] = {
    "x_api_full_archive": 0,
    "xtracker_api": 1,
    "local_archive": 2,
    "gist_archive_2010_2025": 3,
    "sse_signal_log": 4,
    "twscrape": 5,
}


def _priority(source: str) -> int:
    if source in _SOURCE_PRIORITY:
        return _SOURCE_PRIORITY[source]
    if source.startswith("local_archive:"):
        return _SOURCE_PRIORITY["local_archive"]
    if source.startswith("kaggle:"):
        return 6
    return 9


def merge_sources(*frames: pd.DataFrame) -> pd.DataFrame:
    """Concatenate frames, deduplicate by tweet_id keeping highest-priority source."""
    non_empty = [f for f in frames if not f.empty]
    if not non_empty:
        return pd.DataFrame(columns=BASE_COLS)

    combined = pd.concat(non_empty, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"], utc=True, errors="coerce")
    combined = combined.dropna(subset=["date"])

    combined["_priority"] = combined["source"].apply(_priority)
    combined = combined.sort_values(["tweet_id", "_priority"])

    # Preserve strongest metadata across duplicate sightings of the same tweet_id.
    # Example: if any source marks the tweet as RT, keep it as RT.
    combined["is_retweet"] = (
        combined.groupby("tweet_id")["is_retweet"].transform("max").astype(bool)
    )
    for col in ("retweets", "favorites", "user_friends", "user_followers"):
        combined[col] = combined.groupby("tweet_id")[col].transform("max")

    combined = combined.drop_duplicates(subset=["tweet_id"], keep="first")
    combined = combined.drop(columns=["_priority"])

    combined = combined.sort_values("date").reset_index(drop=True)
    combined["date"] = combined["date"].dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
    return combined


def build_merged(
    sse_path: str = "tweet_signal_events.jsonl",
    x_full_archive_path: str = "data/x_api_full_archive_raw.jsonl",
    local_archive_dir: str = "data/local_archive",
    gist_archive_path: str = "data/gist_elon_2010_2025.json",
    twscrape_path: str = "data/twscrape_raw.jsonl",
    kaggle_dir: str = "data/kaggle",
) -> pd.DataFrame:
    print("Loading X full-archive cache …", flush=True)
    xfull_df = load_x_api_full_archive_data(x_full_archive_path)
    print(f"  x_api_full_archive:{len(xfull_df):>7,} tweets")

    print("Loading xtracker …", flush=True)
    xtracker_df = pd.DataFrame(columns=BASE_COLS)
    try:
        xtracker_df = load_xtracker_posts()
        print(f"  xtracker:      {len(xtracker_df):>7,} tweets")
    except Exception as exc:
        print(f"  xtracker failed: {exc}", file=sys.stderr)

    print("Loading local archive …", flush=True)
    local_archive_df = load_local_archive_data(local_archive_dir)
    print(f"  local_archive: {len(local_archive_df):>7,} tweets")

    print("Loading gist archive …", flush=True)
    gist_df = load_gist_archive(gist_archive_path)
    print(f"  gist_archive:  {len(gist_df):>7,} tweets")

    print("Loading SSE log …", flush=True)
    sse_df = load_sse_rows(sse_path)
    print(f"  sse_signal_log:{len(sse_df):>7,} tweets")

    print("Loading twscrape …", flush=True)
    twscrape_df = load_twscrape_data(twscrape_path)
    print(f"  twscrape:      {len(twscrape_df):>7,} tweets")

    print("Loading Kaggle datasets …", flush=True)
    kaggle_df = load_kaggle_datasets(kaggle_dir)
    print(f"  kaggle:        {len(kaggle_df):>7,} tweets")

    merged = merge_sources(
        xfull_df,
        xtracker_df,
        local_archive_df,
        gist_df,
        sse_df,
        twscrape_df,
        kaggle_df,
    )

    dates = pd.to_datetime(merged["date"], utc=True, errors="coerce")
    print(
        f"\nMerged total: {len(merged):,} unique tweets | "
        f"{dates.min().date()} → {dates.max().date()}"
    )
    return merged


def save_cache(df: pd.DataFrame, path: Path = _CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path, index=False)
        print(f"Cached {len(df):,} tweets → {path}")
    except ImportError as exc:
        print(f"Skipping parquet cache write ({exc})")


def load_merged(
    cache_path: str | Path = _CACHE_PATH,
    refresh: bool = False,
    sse_path: str = "tweet_signal_events.jsonl",
    x_full_archive_path: str = "data/x_api_full_archive_raw.jsonl",
    local_archive_dir: str = "data/local_archive",
    gist_archive_path: str = "data/gist_elon_2010_2025.json",
    twscrape_path: str = "data/twscrape_raw.jsonl",
    kaggle_dir: str = "data/kaggle",
) -> pd.DataFrame:
    p = Path(cache_path)
    if not refresh and p.exists():
        try:
            df = pd.read_parquet(p)
            dates = pd.to_datetime(df["date"], utc=True, errors="coerce")
            print(
                f"Loaded {len(df):,} tweets from cache | "
                f"{dates.min().date()} → {dates.max().date()}"
            )
            return df
        except ImportError as exc:
            print(f"Parquet cache unavailable ({exc}); rebuilding from sources …")

    df = build_merged(
        sse_path=sse_path,
        x_full_archive_path=x_full_archive_path,
        local_archive_dir=local_archive_dir,
        gist_archive_path=gist_archive_path,
        twscrape_path=twscrape_path,
        kaggle_dir=kaggle_dir,
    )
    save_cache(df, p)
    return df
