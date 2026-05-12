from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from .base import BASE_COLS, standardize_frame

DEFAULT_LOCAL_ARCHIVE_DIR = "data/local_archive"


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _pick_date(obj: dict[str, Any]) -> Any:
    return obj.get("created_at") or obj.get("createdAt") or obj.get("date")


def _is_retweet(obj: dict[str, Any], text: str) -> bool:
    if text.startswith("RT @"):
        return True
    if obj.get("retweetedTweet"):
        return True
    refs = obj.get("referencedTweets") or obj.get("referenced_tweets") or []
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, dict) and str(ref.get("type") or "").lower() == "retweeted":
                return True
    return False


def _rows_from_items(items: list[dict[str, Any]], source_file: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    source_tag = f"local_archive:{source_file}"
    for obj in items:
        if not isinstance(obj, dict):
            continue

        text = str(obj.get("text") or obj.get("full_text") or "")
        stats = obj.get("stats") or {}
        out.append(
            {
                "tweet_id": str(obj.get("id") or obj.get("tweet_id") or ""),
                "text": text,
                "date": _pick_date(obj),
                "source": source_tag,
                "retweets": _to_int(
                    obj.get("retweetCount")
                    or obj.get("retweets")
                    or stats.get("retweets")
                    or stats.get("retweet_count")
                ),
                "favorites": _to_int(
                    obj.get("likeCount")
                    or obj.get("favoriteCount")
                    or obj.get("favorites")
                    or stats.get("likes")
                    or stats.get("favorite_count")
                ),
                "user_friends": 0,
                "user_followers": 0,
                "is_retweet": _is_retweet(obj, text),
            }
        )
    return out


def _load_single_json(path: Path) -> pd.DataFrame:
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        print(f"  [warn] local archive parse failed {path}: {exc}", file=sys.stderr)
        return pd.DataFrame(columns=BASE_COLS)

    if not isinstance(payload, list):
        return pd.DataFrame(columns=BASE_COLS)

    rows = _rows_from_items(payload, path.name)
    if not rows:
        return pd.DataFrame(columns=BASE_COLS)
    return standardize_frame(pd.DataFrame(rows))


def load_local_archive_data(data_dir: str = DEFAULT_LOCAL_ARCHIVE_DIR) -> pd.DataFrame:
    """Load local archive JSON exports (e.g. elonmusk-ALL*.json) from data_dir."""
    d = Path(data_dir)
    if not d.is_dir():
        return pd.DataFrame(columns=BASE_COLS)

    frames: list[pd.DataFrame] = []
    for path in sorted(d.glob("*.json")):
        frame = _load_single_json(path)
        if frame.empty:
            continue
        frames.append(frame)
        print(f"  Loaded {len(frame):,} rows from {path}")

    if not frames:
        return pd.DataFrame(columns=BASE_COLS)

    combined = pd.concat(frames, ignore_index=True)
    return standardize_frame(combined)
