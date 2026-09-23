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


def _get(url: str, settings: dict, **kwargs) -> dict:
    headers = {"User-Agent": settings.get("user_agent", "hype-index/1.0")}
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


COLLECTORS = {
    "reddit_subscribers": reddit_subscribers,
    "reddit_daily_posts": reddit_daily_posts,
    "wikipedia_pageviews": wikipedia_pageviews,
    "youtube_daily_uploads": youtube_daily_uploads,
}
MANUAL = "manual"
