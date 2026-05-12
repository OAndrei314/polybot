from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

from .base import BASE_COLS, standardize_frame

# Known Kaggle datasets covering Elon Musk's tweet history.
# Each entry is (dataset_slug, expected_date_coverage).
KAGGLE_DATASETS: list[tuple[str, str]] = [
    ("gpreda/elon-musk-tweets", "2010-2022"),
    ("jessmcelveen/elon-musk-tweets", "2010-2022"),
    ("vidyapb/elon-musk-tweets-2015-2022", "2015-2022"),
    ("ayhmrsd/elon-musk-tweets", "2010-2021"),
]

# Column-name variants across different Kaggle datasets → canonical name.
# Priority: first match wins when multiple source columns map to the same target.
_COL_PRIORITY: list[tuple[str, str]] = [
    # tweet id
    ("tweet_id", "tweet_id"),
    ("id_str", "tweet_id"),
    ("id", "tweet_id"),
    ("musk_tweet_id", "tweet_id"),
    # text (prefer longer/fuller versions)
    ("fullText", "text"),
    ("full_text", "text"),
    ("musk_quote_tweet", "text"),
    ("tweet", "text"),
    ("text", "text"),
    # date (prefer full datetime over date-only)
    ("createdAt", "date"),
    ("created_at", "date"),
    ("musk_quote_created_at", "date"),
    ("Timestamp", "date"),
    ("timestamp", "date"),
    ("date", "date"),
    # engagement
    ("retweet_count", "retweets"),
    ("retweetCount", "retweets"),
    ("musk_quote_retweet_count", "retweets"),
    ("retweets_count", "retweets"),
    ("retweets", "retweets"),
    ("favorite_count", "favorites"),
    ("likeCount", "favorites"),
    ("musk_quote_like_count", "favorites"),
    ("likes_count", "favorites"),
    ("likes", "favorites"),
    ("favorites", "favorites"),
    # retweet flag
    ("isRetweet", "is_retweet"),
    ("is_retweet", "is_retweet"),
    ("retweeted", "is_retweet"),
    ("retweet", "is_retweet"),
]


def _normalize_csv(raw: pd.DataFrame, source_tag: str) -> pd.DataFrame:
    """Map arbitrary Kaggle CSV columns to BASE_COLS, first-match-wins to avoid duplicates."""
    cols = set(raw.columns)
    rename: dict[str, str] = {}
    claimed_targets: set[str] = set()

    for src, tgt in _COL_PRIORITY:
        if src in cols and tgt not in claimed_targets:
            rename[src] = tgt
            claimed_targets.add(tgt)

    df = raw[list(rename.keys())].rename(columns=rename).copy()

    if "tweet_id" not in df.columns:
        df["tweet_id"] = raw.index.astype(str)

    df["source"] = source_tag
    return standardize_frame(df)


def download_kaggle_datasets(out_dir: str = "data/kaggle") -> Path:
    """Download all KAGGLE_DATASETS via the kaggle CLI into out_dir."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(["kaggle", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print(
            "kaggle CLI not found. Install it:\n"
            "  pip install kaggle\n"
            "Then place your API key at ~/.kaggle/kaggle.json\n"
            "  https://www.kaggle.com/docs/api\n",
            file=sys.stderr,
        )
        return out

    for slug, coverage in KAGGLE_DATASETS:
        name = slug.split("/")[-1]
        dest = out / name
        if dest.exists() and any(dest.glob("*.csv")):
            print(f"  [skip] {slug} already downloaded ({dest})")
            continue
        dest.mkdir(parents=True, exist_ok=True)
        print(f"  Downloading {slug} ({coverage}) → {dest}")
        result = subprocess.run(
            ["kaggle", "datasets", "download", "-d", slug, "-p", str(dest), "--unzip"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  [warn] kaggle download failed for {slug}:\n{result.stderr}", file=sys.stderr)
        else:
            csvs = list(dest.glob("*.csv"))
            print(f"  OK — {len(csvs)} CSV file(s) in {dest}")

    return out


def load_kaggle_datasets(data_dir: str = "data/kaggle") -> pd.DataFrame:
    """Load all downloaded Kaggle CSVs and the /kaggle/input fallback."""
    search_dirs: list[Path] = [Path(data_dir)]

    # Support running inside a Kaggle notebook environment.
    kaggle_input = Path("/kaggle/input/elon-musk-tweets")
    if kaggle_input.is_dir():
        search_dirs.append(kaggle_input)

    frames: list[pd.DataFrame] = []
    seen_files: set[str] = set()

    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for csv_path in sorted(directory.rglob("*.csv")):
            key = csv_path.name
            if key in seen_files:
                continue
            seen_files.add(key)
            try:
                raw = pd.read_csv(csv_path, low_memory=False)
                source_tag = f"kaggle:{csv_path.parent.name}"
                frames.append(_normalize_csv(raw, source_tag))
                print(f"  Loaded {len(raw):,} rows from {csv_path}")
            except Exception as exc:
                print(f"  [warn] could not parse {csv_path}: {exc}", file=sys.stderr)

    if not frames:
        return pd.DataFrame(columns=BASE_COLS)

    combined = pd.concat(frames, ignore_index=True)
    return standardize_frame(combined)
