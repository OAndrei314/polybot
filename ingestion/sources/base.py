from __future__ import annotations

import pandas as pd

BASE_COLS: list[str] = [
    "tweet_id",
    "text",
    "date",
    "source",
    "retweets",
    "favorites",
    "user_friends",
    "user_followers",
    "is_retweet",
]

# Elon joined Twitter in 2009. Keep the historical window focused on this project.
MIN_VALID_DATE = pd.Timestamp("2009-01-01 00:00:00+00:00")
FUTURE_SKEW = pd.Timedelta(days=2)


def _parse_datetime_utc(values: pd.Series) -> pd.Series:
    """Best-effort but deterministic datetime parser across mixed source formats."""
    s = values.astype(str).str.strip()
    dt = pd.to_datetime(s, utc=True, errors="coerce", format="ISO8601")

    if dt.isna().any():
        missing = dt.isna()
        for fmt in (
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%a %b %d %H:%M:%S %z %Y",
        ):
            parsed = pd.to_datetime(s[missing], utc=True, errors="coerce", format=fmt)
            dt.loc[missing] = dt.loc[missing].fillna(parsed)
            missing = dt.isna()
            if not missing.any():
                break
    return dt


def standardize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize any raw tweet DataFrame to BASE_COLS with consistent dtypes."""
    if df is None or df.empty:
        return pd.DataFrame(columns=BASE_COLS)

    out = df.copy()

    if "tweet_id" not in out.columns:
        out["tweet_id"] = ""
    out["tweet_id"] = out["tweet_id"].astype(str)

    if "text" not in out.columns:
        out["text"] = ""
    out["text"] = out["text"].fillna("").astype(str)

    if "date" not in out.columns:
        out["date"] = pd.NaT
    out["date"] = _parse_datetime_utc(out["date"])
    now_utc = pd.Timestamp.now(tz="UTC")
    out = out[(out["date"] >= MIN_VALID_DATE) & (out["date"] <= now_utc + FUTURE_SKEW)]

    if "source" not in out.columns:
        out["source"] = "unknown"
    out["source"] = out["source"].fillna("unknown").astype(str)

    for col in ("retweets", "favorites", "user_friends", "user_followers"):
        if col not in out.columns:
            out[col] = 0
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)

    text_retweet = out["text"].str.startswith("RT @")
    if "is_retweet" not in out.columns:
        out["is_retweet"] = text_retweet
    else:
        # Some sources under-report the RT flag; keep explicit source flag OR text heuristic.
        src_flag = pd.Series(out["is_retweet"], dtype="boolean").fillna(False).astype(bool)
        out["is_retweet"] = src_flag | text_retweet

    out = out.dropna(subset=["date"])
    out = out[BASE_COLS].copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d %H:%M:%S+00:00")
    return out.reset_index(drop=True)
