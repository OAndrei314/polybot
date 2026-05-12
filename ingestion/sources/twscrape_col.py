"""
Scrape Elon Musk's full Twitter/X history via twscrape.

twscrape uses real X accounts (not the paid API) and handles pagination
automatically. Each account can fetch ~50 tweets/request before sleeping.
Multiple accounts are pooled to maximise throughput.

Setup (one-time):
    pip install twscrape
    # Create accounts.txt with one line per throwaway X account:
    #   username:password:email:email_password
    python -m tweet_sources.twscrape_col --accounts accounts.txt --login
    python -m tweet_sources.twscrape_col --since 2022-01-01 --until 2025-10-31
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pandas as pd

from .base import BASE_COLS, standardize_frame

ELON_USER_ID = 44196397
_DEFAULT_DB = "data/twscrape.db"
_DEFAULT_OUT = "data/twscrape_raw.jsonl"
_PROGRESS_EVERY = 500


def _patch_xclid() -> None:
    """X changed their JS bundle format, breaking twscrape's xclid parser (IndexError).
    Replace XClIdGen with a version that generates a valid-format header without fetching X's homepage."""
    import base64, hashlib, math, random, time
    import twscrape.xclid as _x
    import twscrape.queue_client as _qc

    class _DummyXClIdGen:
        def __init__(self) -> None:
            self.vk_bytes = [random.randint(0, 255) for _ in range(16)]
            self.anim_key = "0.0"

        def calc(self, method: str, path: str) -> str:
            ts = math.floor((time.time() * 1000 - 1682924400 * 1000) / 1000)
            ts_bytes = [(ts >> (i * 8)) & 0xFF for i in range(4)]
            dkw, drn = "obfiowerehiring", 3
            pld = f"{method.upper()}!{path}!{ts}{dkw}{self.anim_key}"
            pld = list(hashlib.sha256(pld.encode()).digest())
            pld = [*self.vk_bytes, *ts_bytes, *pld[:16], drn]
            num = random.randint(0, 255)
            pld = bytearray([num, *[x ^ num for x in pld]])
            return base64.b64encode(pld).decode("utf-8").strip("=")

        @staticmethod
        async def create(clt=None) -> "_DummyXClIdGen":
            return _DummyXClIdGen()

    _x.XClIdGen = _DummyXClIdGen  # type: ignore[assignment]
    _qc.XClIdGen = _DummyXClIdGen  # type: ignore[assignment]


def _tweet_to_row(t: object) -> dict:
    """Convert a twscrape Tweet object to a BASE_COLS-compatible dict."""
    text = getattr(t, "rawContent", None) or getattr(t, "content", "") or ""
    date = getattr(t, "date", None)
    user = getattr(t, "user", None)
    retweeted = getattr(t, "retweetedTweet", None)
    return {
        "tweet_id": str(getattr(t, "id", "")),
        "text": text,
        "date": date.isoformat() if date else None,
        "source": "twscrape",
        "retweets": int(getattr(t, "retweetCount", 0) or 0),
        "favorites": int(getattr(t, "likeCount", 0) or 0),
        "user_friends": int(getattr(user, "friendsCount", 0) or 0) if user else 0,
        "user_followers": int(getattr(user, "followersCount", 0) or 0) if user else 0,
        "is_retweet": retweeted is not None,
    }


async def _iter_user_tweets(api: object, since: datetime | None, until: datetime | None) -> AsyncIterator[dict]:
    """Yield tweet dicts from Elon's timeline in reverse-chronological order."""
    async for tweet in api.user_tweets(ELON_USER_ID):  # type: ignore[attr-defined]
        date = getattr(tweet, "date", None)
        if date is None:
            continue
        if until is not None and date > until:
            continue
        if since is not None and date < since:
            return  # tweets are newest-first; stop when we go past the since date
        yield _tweet_to_row(tweet)


async def _run_scrape(
    accounts_file: str,
    db_path: str,
    out_path: str,
    since: datetime | None,
    until: datetime | None,
    login: bool,
) -> None:
    try:
        from twscrape import API  # type: ignore[import]
    except ImportError:
        print("twscrape not installed. Run: pip install twscrape", file=sys.stderr)
        sys.exit(1)

    # X changed their JS bundle format, breaking twscrape's xclid parser.
    # Patch XClIdGen to generate a syntactically valid header without fetching X's homepage.
    _patch_xclid()

    api = API(db_path)

    if login or accounts_file:
        acct_path = Path(accounts_file) if accounts_file else None
        if acct_path and acct_path.exists():
            await api.pool.load_from_file(str(acct_path), "username:password:email:email_password")  # type: ignore[attr-defined]
            if login:
                await api.pool.login_all()  # type: ignore[attr-defined]
                print("Accounts logged in and saved to DB.")
                return
        else:
            print(
                f"Accounts file not found: {accounts_file}\n"
                "Create it with one line per account:\n"
                "  username:password:email:email_password\n",
                file=sys.stderr,
            )
            sys.exit(1)

    accounts = await api.pool.get_all()  # type: ignore[attr-defined]
    if not accounts:
        print(
            "No accounts in the twscrape pool.\n"
            "Add accounts and login first:\n"
            "  python -m tweet_sources.twscrape_col --accounts accounts.txt --login\n",
            file=sys.stderr,
        )
        sys.exit(1)

    active = [a for a in accounts if getattr(a, "active", False)]
    print(f"Pool: {len(active)}/{len(accounts)} accounts active")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    existing_ids: set[str] = set()
    if out.exists():
        with open(out) as fh:
            for line in fh:
                try:
                    existing_ids.add(json.loads(line.strip())["tweet_id"])
                except Exception:
                    pass
        print(f"Resuming — {len(existing_ids):,} tweets already on disk")

    count = 0
    skipped = 0
    with open(out, "a") as fh:
        async for row in _iter_user_tweets(api, since, until):
            tid = row["tweet_id"]
            if tid in existing_ids:
                skipped += 1
                continue
            existing_ids.add(tid)
            fh.write(json.dumps(row) + "\n")
            count += 1
            if count % _PROGRESS_EVERY == 0:
                date_str = row.get("date", "?")[:10]
                print(f"  {count:,} new tweets scraped … latest date: {date_str}")

    print(f"Done. {count:,} new tweets saved ({skipped:,} skipped duplicates)")


def load_twscrape_data(path: str = _DEFAULT_OUT) -> pd.DataFrame:
    """Load a previously scraped twscrape JSONL file into a standardized DataFrame."""
    p = Path(path)
    if not p.exists():
        return pd.DataFrame(columns=BASE_COLS)

    rows: list[dict] = []
    with open(p) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass

    if not rows:
        return pd.DataFrame(columns=BASE_COLS)

    df = pd.DataFrame(rows)
    df["source"] = "twscrape"
    return standardize_frame(df)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape Elon Musk tweets via twscrape")
    parser.add_argument("--accounts", default="accounts.txt", help="Path to accounts.txt")
    parser.add_argument("--db", default=_DEFAULT_DB, help="twscrape SQLite DB path")
    parser.add_argument("--out", default=_DEFAULT_OUT, help="Output JSONL path")
    parser.add_argument("--since", default=None, help="Start date ISO (e.g. 2022-01-01)")
    parser.add_argument("--until", default=None, help="End date ISO (e.g. 2025-10-31)")
    parser.add_argument("--login", action="store_true", help="Login accounts and exit")
    args = parser.parse_args()

    asyncio.run(
        _run_scrape(
            accounts_file=args.accounts,
            db_path=args.db,
            out_path=args.out,
            since=_parse_dt(args.since),
            until=_parse_dt(args.until),
            login=args.login,
        )
    )
