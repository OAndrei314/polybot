"""
Full tweet history collection pipeline.

Steps:
  0. [--x-full-archive] Fetch full history via official X `/2/tweets/search/all` (paid access)
  0b.[--gist-archive] Download public external 2010–2025 archive JSON
  1. [--kaggle]    Download Kaggle datasets (2010–2022) via kaggle CLI
  2. [--twscrape]  Scrape Twitter/X for the 2022–2025 gap via twscrape
  3.               Merge all sources → data/tweets_all.parquet

Usage examples:

  # First time: download Kaggle + scrape the gap
  python -m ingestion.collect --kaggle --twscrape --accounts accounts.txt

  # If you have paid X API access, pull full archive directly:
  python -m ingestion.collect --x-full-archive --x-start-time 2009-01-01T00:00:00Z

  # Pull external 2010–2025 archive (free/public):
  python -m ingestion.collect --gist-archive

  # Login twscrape accounts (one-time, run before --twscrape):
  python -m ingestion.collect --login --accounts accounts.txt

  # Rebuild merged parquet from already-downloaded data:
  python -m ingestion.collect

  # Force full rebuild (ignore existing cache):
  python -m ingestion.collect --refresh

accounts.txt format (one line per throwaway X account):
  username:password:email:email_password
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone


def _dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect and merge full Elon Musk tweet history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--kaggle", action="store_true", help="Download Kaggle datasets")
    parser.add_argument("--kaggle-dir", default="data/kaggle")
    parser.add_argument(
        "--x-full-archive",
        action="store_true",
        help="Fetch via official X full-archive endpoint (/2/tweets/search/all)",
    )
    parser.add_argument("--x-query", default="from:elonmusk")
    parser.add_argument("--x-start-time", default="2009-01-01T00:00:00Z")
    parser.add_argument("--x-end-time", default=None)
    parser.add_argument("--x-max-results", type=int, default=500)
    parser.add_argument("--x-max-pages", type=int, default=None)
    parser.add_argument("--x-pause-s", type=float, default=0.25)
    parser.add_argument("--x-overwrite", action="store_true")
    parser.add_argument("--x-out", default="data/x_api_full_archive_raw.jsonl")
    parser.add_argument(
        "--gist-archive",
        action="store_true",
        help="Download public external 2010–2025 Elon archive JSON",
    )
    parser.add_argument(
        "--gist-url",
        default="https://gist.githubusercontent.com/j4ckxyz/1518890aa6d319d23a516bf14240d2b2/raw",
    )
    parser.add_argument("--gist-out", default="data/gist_elon_2010_2025.json")
    parser.add_argument("--twscrape", action="store_true", help="Scrape via twscrape")
    parser.add_argument("--accounts", default="accounts.txt")
    parser.add_argument("--login", action="store_true", help="Login accounts and exit")
    parser.add_argument("--since", default="2022-01-01")
    parser.add_argument("--until", default="2025-10-31")
    parser.add_argument("--twscrape-out", default="data/twscrape_raw.jsonl")
    parser.add_argument("--twscrape-db", default="data/twscrape.db")
    parser.add_argument("--sse-path", default="tweet_signal_events.jsonl")
    parser.add_argument("--cache", default="data/tweets_all.parquet")
    parser.add_argument("--refresh", action="store_true", help="Ignore existing cache")
    args = parser.parse_args()

    if args.x_full_archive:
        print("\n=== Fetching X full-archive data ===")
        from ingestion.sources.x_api_full_archive import fetch_full_archive
        fetch_full_archive(
            out_path=args.x_out,
            query=args.x_query,
            start_time=args.x_start_time,
            end_time=args.x_end_time,
            max_results=args.x_max_results,
            max_pages=args.x_max_pages,
            pause_s=args.x_pause_s,
            overwrite=args.x_overwrite,
        )

    if args.gist_archive:
        print("\n=== Downloading public gist archive ===")
        from ingestion.sources.gist_archive import download_gist_archive
        download_gist_archive(url=args.gist_url, out_path=args.gist_out)

    if args.kaggle:
        print("\n=== Downloading Kaggle datasets ===")
        from ingestion.sources.kaggle_datasets import download_kaggle_datasets
        download_kaggle_datasets(out_dir=args.kaggle_dir)

    if args.login:
        print("\n=== Logging in twscrape accounts ===")
        import asyncio
        from ingestion.sources.twscrape_col import _run_scrape
        asyncio.run(
            _run_scrape(
                accounts_file=args.accounts,
                db_path=args.twscrape_db,
                out_path=args.twscrape_out,
                since=None,
                until=None,
                login=True,
            )
        )
        print("Login done. Re-run without --login to start scraping.")
        sys.exit(0)

    if args.twscrape:
        print(f"\n=== Scraping via twscrape ({args.since} → {args.until}) ===")
        import asyncio
        from ingestion.sources.twscrape_col import _run_scrape
        asyncio.run(
            _run_scrape(
                accounts_file=args.accounts,
                db_path=args.twscrape_db,
                out_path=args.twscrape_out,
                since=_dt(args.since),
                until=_dt(args.until),
                login=False,
            )
        )

    print("\n=== Merging all sources ===")
    from ingestion.merger import load_merged
    df = load_merged(
        cache_path=args.cache,
        refresh=args.refresh,
        sse_path=args.sse_path,
        x_full_archive_path=args.x_out,
        gist_archive_path=args.gist_out,
        twscrape_path=args.twscrape_out,
        kaggle_dir=args.kaggle_dir,
    )
    print(f"\nReady. {len(df):,} tweets in {args.cache}")


if __name__ == "__main__":
    main()
