"""Proxy-source collectors.

Each collector returns a list of (date, value) observations for complete
Singapore-time days. Collection never affects the formula: it only appends raw
observations, tagged with the metric's source_key.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone

import requests

from .store import SGT


class SourceError(Exception):
    pass


def _sgt_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=SGT)
    return start, start + timedelta(days=1)


def _get(url: str, settings: dict, headers_extra: dict | None = None, **kwargs) -> dict:
    headers = {"User-Agent": settings.get("user_agent", "hype-index/1.0"),
               "Accept": "application/json", **(headers_extra or {})}
    resp = requests.get(url, headers=headers, timeout=settings.get("timeout_seconds", 30), **kwargs)
    if resp.status_code != 200:
        raise SourceError(f"GET {url} -> HTTP {resp.status_code}")
    return resp.json()


def reddit_subscribers(source: dict, day: date, settings: dict) -> list[tuple[date, float]]:
    """Subscriber count snapshot, taken just after `day` ends (SGT)."""
    data = _get(f"https://www.reddit.com/r/{source['subreddit']}/about.json", settings)
    return [(day, float(data["data"]["subscribers"]))]


def reddit_daily_posts(source: dict, day: date, settings: dict) -> list[tuple[date, float]]:
    """Number of posts created in the subreddit during `day` (SGT)."""
    start, end = _sgt_day_bounds(day)
    count, after = 0, None
    for _ in range(10):  # listing API caps at ~1000 items
        params = {"limit": 100, **({"after": after} if after else {})}
        data = _get(f"https://www.reddit.com/r/{source['subreddit']}/new.json", settings, params=params)
        children = data["data"]["children"]
        if not children:
            break
        oldest = None
        for c in children:
            created = datetime.fromtimestamp(c["data"]["created_utc"], tz=timezone.utc)
            oldest = created
            if start <= created < end:
                count += 1
        after = data["data"].get("after")
        if oldest < start or not after:
            break
    return [(day, float(count))]


def wikipedia_pageviews(source: dict, day: date, settings: dict) -> list[tuple[date, float]]:
    """Daily user pageviews. Wikimedia days are UTC and published with a lag, so
    this fetches the last 14 days and returns every day available (backfill)."""
    first = day - timedelta(days=14)
    url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
           f"{source['project']}/all-access/user/{source['article']}/daily/"
           f"{first:%Y%m%d}00/{day:%Y%m%d}00")
    data = _get(url, settings)
    out = []
    for item in data.get("items", []):
        d = datetime.strptime(item["timestamp"][:8], "%Y%m%d").date()
        out.append((d, float(item["views"])))
    return out


def youtube_daily_uploads(source: dict, day: date, settings: dict) -> list[tuple[date, float]]:
    """Number of videos matching `query` published during `day` (SGT)."""
    key = os.environ.get(settings.get("youtube_api_key_env", "YOUTUBE_API_KEY"))
    if not key:
        raise SourceError("YouTube API key env var not set")
    start, end = _sgt_day_bounds(day)
    params = {
        "part": "id", "type": "video", "maxResults": 50, "q": source["query"], "key": key,
        "publishedAfter": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "publishedBefore": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    count = 0
    for _ in range(10):
        data = _get("https://www.googleapis.com/youtube/v3/search", settings, params=params)
        count += len(data.get("items", []))
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return [(day, float(count))]


_official_cache: dict = {}


def _official_events_for_day(source: dict, day: date, settings: dict) -> list[dict]:
    """Every event worldwide on the official Riftbound locator (Carde.io API)
    whose start time falls in `day` (SGT), all statuses. Cached per run so the
    events and players metrics share one crawl."""
    key = (source["api_base"], source["game_slug"], day)
    if key in _official_cache:
        return _official_cache[key]
    start, end = _sgt_day_bounds(day)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = [
        ("game_slug", source["game_slug"]),
        ("start_date_after", start.astimezone(timezone.utc).strftime(fmt)),
        ("start_date_before", end.astimezone(timezone.utc).strftime(fmt)),
        ("display_statuses", "upcoming"), ("display_statuses", "inProgress"),
        ("display_statuses", "past"),
        ("page_size", 100),
    ]
    events, page = [], 1
    while True:
        data = _get(f"{source['api_base']}/events/", settings,
                    params=params + [("page", page)],
                    headers_extra={"Referer": source["referer"], "Origin": source["referer"].rstrip("/")})
        events += data.get("results", [])
        nxt = data.get("next_page_number")
        if not nxt:
            break
        if page >= 500:
            # Never silently truncate: a partial count would corrupt the series.
            raise SourceError(f"more than {page} pages of events for {day}; refusing partial count")
        page = nxt
    expected = data.get("count")
    if expected is not None and expected != len(events):
        raise SourceError(f"API reported {expected} events for {day} but {len(events)} were returned")
    _official_cache[key] = events
    return events


def riftbound_official_events(source: dict, day: date, settings: dict) -> list[tuple[date, float]]:
    """Number of official Riftbound events worldwide starting on `day` - lag_days.

    The lag gives stores time to finish and report events before they are counted."""
    target = day - timedelta(days=source["lag_days"])
    return [(target, float(len(_official_events_for_day(source, target, settings))))]


def riftbound_official_players(source: dict, day: date, settings: dict) -> list[tuple[date, float]]:
    """Total starting players across those events (missing counts as 0)."""
    target = day - timedelta(days=source["lag_days"])
    events = _official_events_for_day(source, target, settings)
    return [(target, float(sum(e.get("starting_player_count") or 0 for e in events)))]


COLLECTORS = {
    "riftbound_official_events": riftbound_official_events,
    "riftbound_official_players": riftbound_official_players,
    "reddit_subscribers": reddit_subscribers,
    "reddit_daily_posts": reddit_daily_posts,
    "wikipedia_pageviews": wikipedia_pageviews,
    "youtube_daily_uploads": youtube_daily_uploads,
}
MANUAL = "manual"
