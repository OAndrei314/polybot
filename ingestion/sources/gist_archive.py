from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .base import BASE_COLS, standardize_frame

DEFAULT_GIST_URL = "https://gist.githubusercontent.com/j4ckxyz/1518890aa6d319d23a516bf14240d2b2/raw"
DEFAULT_GIST_PATH = "data/gist_elon_2010_2025.json"


def download_gist_archive(
    url: str = DEFAULT_GIST_URL,
    out_path: str = DEFAULT_GIST_PATH,
    timeout: int = 90,
) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    out.write_text(resp.text)
    print(f"Downloaded gist archive -> {out} ({len(resp.text):,} bytes)")
    return out


def _to_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for obj in items:
        if not isinstance(obj, dict):
            continue

        stats = obj.get("stats") or {}
        text = str(obj.get("text", "") or "")
        rows.append(
            {
                "tweet_id": str(obj.get("id", "") or ""),
                "text": text,
                "date": obj.get("created_at"),
                "source": "gist_archive_2010_2025",
                "retweets": int(stats.get("retweets", 0) or 0),
                "favorites": int(stats.get("likes", 0) or 0),
                "user_friends": 0,
                "user_followers": 0,
                "is_retweet": text.startswith("RT @"),
            }
        )
    return rows


def load_gist_archive(path: str = DEFAULT_GIST_PATH) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=BASE_COLS)

    try:
        items = json.loads(p.read_text())
    except Exception:
        return pd.DataFrame(columns=BASE_COLS)

    if not isinstance(items, list):
        return pd.DataFrame(columns=BASE_COLS)

    rows = _to_rows(items)
    if not rows:
        return pd.DataFrame(columns=BASE_COLS)
    return standardize_frame(pd.DataFrame(rows))


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Elon 2010-2025 gist archive")
    parser.add_argument("--url", default=DEFAULT_GIST_URL)
    parser.add_argument("--out", default=DEFAULT_GIST_PATH)
    args = parser.parse_args()

    download_gist_archive(url=args.url, out_path=args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
