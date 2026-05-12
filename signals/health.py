from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import requests

from market.prices import EVENT_SLUGS

XTRACKER_BASE = "https://xtracker.polymarket.com/api"
USERNAME = "elonmusk"


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


class XTrackerHealth:
    """Checks if SSE is alive and XTracker fallback is fresh."""

    def __init__(
        self,
        event_slugs: list[str] | None = None,
        timeout_s: float = 5.0,
        trackings_active_only: bool = False,
    ) -> None:
        self.event_slugs = list(event_slugs or EVENT_SLUGS)
        self.timeout_s = timeout_s
        self.trackings_active_only = bool(trackings_active_only)
        self.session = requests.Session()

    def set_event_slugs(self, event_slugs: list[str]) -> None:
        seen: set[str] = set()
        out: list[str] = []
        for slug in event_slugs:
            s = str(slug or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        self.event_slugs = out

    def _trackings(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        active = "true" if self.trackings_active_only else "false"
        try:
            r = self.session.get(
                f"{XTRACKER_BASE}/users/{USERNAME}/trackings?activeOnly={active}",
                timeout=self.timeout_s,
            )
            if not r.ok:
                return out
            payload = r.json()
            for tr in payload.get("data") or []:
                link = str(tr.get("marketLink") or "")
                slug = link.rstrip("/").rsplit("/", 1)[-1] if link else ""
                if slug:
                    out[slug] = tr
        except Exception:
            return out
        return out

    def _period_posts(self, start: datetime, end: datetime) -> dict[str, Any]:
        url = (
            f"{XTRACKER_BASE}/users/{USERNAME}/posts"
            f"?startDate={urllib.parse.quote(_iso_z(start))}"
            f"&endDate={urllib.parse.quote(_iso_z(end))}"
        )
        t0 = time.time()
        try:
            r = self.session.get(url, timeout=self.timeout_s)
            http_ms = (time.time() - t0) * 1000.0
            if not r.ok:
                return {"ok": False, "status": r.status_code, "http_ms": round(http_ms, 1)}
            payload = r.json()
            posts = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(posts, list):
                return {"ok": False, "status": r.status_code, "http_ms": round(http_ms, 1)}

            latest = posts[0] if posts else {}
            created = _parse_iso(latest.get("createdAt") if isinstance(latest, dict) else None)
            imported = _parse_iso(latest.get("importedAt") if isinstance(latest, dict) else None)
            lag_s = None
            if created is not None and imported is not None:
                lag_s = max(0.0, (imported - created).total_seconds())

            return {
                "ok": True,
                "status": r.status_code,
                "http_ms": round(http_ms, 1),
                "count": len(posts),
                "latest_post_id": latest.get("platformId") if isinstance(latest, dict) else None,
                "latest_created_at": created.isoformat() if created else None,
                "latest_imported_at": imported.isoformat() if imported else None,
                "import_lag_s": round(lag_s, 3) if lag_s is not None else None,
            }
        except Exception as exc:
            return {"ok": False, "status": None, "error": str(exc)[:200], "http_ms": None}

    def poll(self, tweet_state: dict[str, Any]) -> dict[str, Any]:
        trackings = self._trackings()
        events: dict[str, dict[str, Any]] = {}

        for slug in self.event_slugs:
            tr = trackings.get(slug)
            if not tr:
                events[slug] = {"ok": False, "error": "tracking_not_found"}
                continue
            start = _parse_iso(tr.get("startDate"))
            end = _parse_iso(tr.get("endDate"))
            if start is None or end is None:
                events[slug] = {"ok": False, "error": "bad_tracking_window"}
                continue
            snapshot = self._period_posts(start, end)
            snapshot["start"] = start.isoformat()
            snapshot["end"] = end.isoformat()
            snapshot["tracking_active"] = bool(tr.get("isActive"))
            events[slug] = snapshot

        sse = tweet_state.get("sse") or {}
        ready = bool(sse.get("ready"))
        heartbeat_age = sse.get("heartbeat_age_s")
        sse_alive = ready and isinstance(heartbeat_age, (int, float)) and heartbeat_age < 45

        ok_events = [slug for slug, row in events.items() if row.get("ok")]
        xtracker_alive = len(ok_events) >= max(1, len(self.event_slugs) // 2)

        if sse_alive:
            status = "healthy"
        elif xtracker_alive:
            status = "degraded_sse_using_xtracker"
        else:
            status = "degraded_all"

        return {
            "status": status,
            "sse_alive": sse_alive,
            "sse_ready": ready,
            "sse_heartbeat_age_s": heartbeat_age,
            "xtracker_alive": xtracker_alive,
            "ok_event_count": len(ok_events),
            "event_count": len(self.event_slugs),
            "events": events,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


if __name__ == "__main__":
    from signals.stream import TweetStream

    stream = TweetStream()
    stream.start()
    checker = XTrackerHealth()
    while True:
        out = checker.poll(stream.poll())
        print(json.dumps(out, indent=2))
        time.sleep(10)
