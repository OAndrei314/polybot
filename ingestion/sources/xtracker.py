from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import requests

from .base import BASE_COLS, standardize_frame

XTRACKER_BASE = "https://xtracker.polymarket.com/api"
ELON_USERNAME = "elonmusk"


def load_xtracker_posts(
    username: str = ELON_USERNAME,
    since: str = "2009-01-01T00:00:00Z",
    until: str | None = None,
    timeout: int = 45,
) -> pd.DataFrame:
    """Fetch all posts for a user from the xtracker Polymarket API."""
    if until is None:
        until = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    resp = requests.get(
        f"{XTRACKER_BASE}/users/{username}/posts",
        params={"startDate": since, "endDate": until},
        timeout=timeout,
    )
    resp.raise_for_status()

    payload = resp.json() if resp.text else {}
    rows = payload.get("data") if isinstance(payload, dict) else []
    if not rows:
        return pd.DataFrame(columns=BASE_COLS)

    raw = pd.DataFrame(rows)
    content = raw.get("content", pd.Series([""] * len(raw))).fillna("")
    df = pd.DataFrame(
        {
            "tweet_id": raw.get("platformId", pd.Series([""] * len(raw))).astype(str),
            "text": content,
            "date": raw.get("createdAt"),
            "source": "xtracker_api",
            "retweets": 0,
            "favorites": 0,
            "user_friends": 0,
            "user_followers": 0,
            "is_retweet": content.astype(str).str.startswith("RT @"),
        }
    )
    return standardize_frame(df)
