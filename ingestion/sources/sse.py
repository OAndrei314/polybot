from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .base import BASE_COLS, standardize_frame

_DEFAULT_PATH = "tweet_signal_events.jsonl"


def load_sse_rows(path: str = _DEFAULT_PATH) -> pd.DataFrame:
    """Load tweets captured via the SSE browser signal log."""
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=BASE_COLS)

    rows: list[dict] = []
    with open(p) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                pass

    raw = pd.DataFrame(rows)
    if raw.empty:
        return pd.DataFrame(columns=BASE_COLS)

    # Prefer tweet_created_at, fall back to snowflake time, then detected_at.
    date_col = raw.get("tweet_created_at")
    for fallback in ("tweet_snowflake_created_at", "detected_at"):
        fb = raw.get(fallback)
        if fb is not None:
            date_col = date_col.fillna(fb) if date_col is not None else fb

    text = raw.get("tweet_text", pd.Series([""] * len(raw))).fillna("")
    df = pd.DataFrame(
        {
            "tweet_id": raw.get("tweet_id", pd.Series([""] * len(raw))).astype(str),
            "text": text,
            "date": date_col,
            "source": "sse_signal_log",
            "retweets": 0,
            "favorites": 0,
            "user_friends": 0,
            "user_followers": 0,
            "is_retweet": text.astype(str).str.startswith("RT @"),
        }
    )
    return standardize_frame(df)
