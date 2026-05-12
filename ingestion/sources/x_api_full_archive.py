from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .base import BASE_COLS, standardize_frame

_ENDPOINT = "https://api.x.com/2/tweets/search/all"
_DEFAULT_OUT = "data/x_api_full_archive_raw.jsonl"
_DEFAULT_QUERY = "from:elonmusk"


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _auth_header(token: str) -> str:
    token = token.strip()
    return token if token.lower().startswith("bearer ") else f"Bearer {token}"


def _tweet_to_row(tweet: dict[str, Any]) -> dict[str, Any]:
    metrics = tweet.get("public_metrics") or {}
    refs = tweet.get("referenced_tweets") or []
    is_rt = any((r or {}).get("type") == "retweeted" for r in refs)
    return {
        "tweet_id": str(tweet.get("id", "")),
        "text": str(tweet.get("text", "") or ""),
        "date": tweet.get("created_at"),
        "source": "x_api_full_archive",
        "retweets": int(metrics.get("retweet_count", 0) or 0),
        "favorites": int(metrics.get("like_count", 0) or 0),
        "user_friends": 0,
        "user_followers": 0,
        "is_retweet": is_rt,
    }


def _rate_limit_sleep_seconds(headers: dict[str, str]) -> float:
    reset_raw = headers.get("x-rate-limit-reset")
    if not reset_raw:
        return 15.0
    try:
        reset_ts = int(float(reset_raw))
        return max(2.0, float(reset_ts - time.time()) + 1.0)
    except Exception:
        return 15.0


def fetch_full_archive(
    out_path: str = _DEFAULT_OUT,
    query: str = _DEFAULT_QUERY,
    start_time: str = "2009-01-01T00:00:00Z",
    end_time: str | None = None,
    max_results: int = 500,
    max_pages: int | None = None,
    pause_s: float = 0.25,
    overwrite: bool = False,
    bearer_token: str | None = None,
) -> Path:
    """
    Pull tweets from X v2 full-archive search endpoint into JSONL.

    Requires paid X API access that supports /2/tweets/search/all.
    """
    token = (bearer_token or os.getenv("X_BEARER_TOKEN") or os.getenv("TWITTER_BEARER") or "").strip()
    if not token:
        raise RuntimeError(
            "Missing bearer token. Set X_BEARER_TOKEN (or TWITTER_BEARER) to use full-archive search."
        )

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if overwrite and out.exists():
        out.unlink()

    existing_ids: set[str] = set()
    if out.exists():
        with open(out) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing_ids.add(str(json.loads(line).get("tweet_id", "")))
                except Exception:
                    pass

    params: dict[str, Any] = {
        "query": query,
        "start_time": start_time,
        "end_time": end_time or _iso_utc_now(),
        "max_results": max(10, min(500, int(max_results))),
        "tweet.fields": "id,text,created_at,public_metrics,referenced_tweets",
    }
    headers = {"Authorization": _auth_header(token), "User-Agent": "polybot/1.0"}

    page = 0
    saved = 0
    skipped = 0
    next_token: str | None = None

    with open(out, "a") as fh:
        while True:
            page += 1
            if next_token:
                params["next_token"] = next_token
            elif "next_token" in params:
                del params["next_token"]

            resp = requests.get(_ENDPOINT, headers=headers, params=params, timeout=60)
            if resp.status_code == 429:
                sleep_s = _rate_limit_sleep_seconds(resp.headers)
                print(f"Rate limited (429). Sleeping {sleep_s:.1f}s…")
                time.sleep(sleep_s)
                page -= 1
                continue
            resp.raise_for_status()

            payload = resp.json() if resp.text else {}
            data = payload.get("data") if isinstance(payload, dict) else None
            meta = payload.get("meta") if isinstance(payload, dict) else None
            rows = data if isinstance(data, list) else []
            meta = meta if isinstance(meta, dict) else {}

            for tweet in rows:
                row = _tweet_to_row(tweet if isinstance(tweet, dict) else {})
                tid = row["tweet_id"]
                if not tid or tid in existing_ids:
                    skipped += 1
                    continue
                existing_ids.add(tid)
                fh.write(json.dumps(row) + "\n")
                saved += 1

            newest = meta.get("newest_id")
            oldest = meta.get("oldest_id")
            print(
                f"page={page} rows={len(rows)} saved={saved} skipped={skipped} "
                f"newest={newest} oldest={oldest}"
            )

            next_token = meta.get("next_token")
            if not next_token:
                break
            if max_pages is not None and page >= max_pages:
                print(f"Reached max_pages={max_pages}, stopping.")
                break
            time.sleep(max(0.0, pause_s))

    print(f"Done. Saved {saved:,} rows to {out}")
    return out


def load_x_api_full_archive_data(path: str = _DEFAULT_OUT) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=BASE_COLS)

    rows: list[dict[str, Any]] = []
    with open(p) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if not rows:
        return pd.DataFrame(columns=BASE_COLS)

    df = pd.DataFrame(rows)
    df["source"] = "x_api_full_archive"
    return standardize_frame(df)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Elon tweets via X full-archive search endpoint")
    parser.add_argument("--out", default=_DEFAULT_OUT)
    parser.add_argument("--query", default=_DEFAULT_QUERY)
    parser.add_argument("--start-time", default="2009-01-01T00:00:00Z")
    parser.add_argument("--end-time", default=None)
    parser.add_argument("--max-results", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--pause-s", type=float, default=0.25)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    fetch_full_archive(
        out_path=args.out,
        query=args.query,
        start_time=args.start_time,
        end_time=args.end_time,
        max_results=args.max_results,
        max_pages=args.max_pages,
        pause_s=args.pause_s,
        overwrite=args.overwrite,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
