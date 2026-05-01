"""
YouTube stats monitor. Polls public statistics for our uploaded videos
and stores time-series in the video_stats table.

Why this matters: closes the feedback loop. Without stats, the Opus
meta-improver is reviewing prompts blind ("does this script feel
hooky?"). With stats, it has evidence ("this title style got 4x more
views than that one — bias toward this style"). Compounding gain.

Auth model:
  - Uses an API key (different from OAuth). Public statistics endpoint
    only — no user identity, no scope creep, no token refresh.
  - User creates one at console.cloud.google.com/apis/credentials in
    the same launchpad-yt project. ~1 minute.

Quota: stats reads are 1 unit each. Default daily quota is 10,000
units. Even polling every video three times a day, we use a few dozen
units per day — nowhere near the limit.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

import state
from config import YOUTUBE_API_KEY


YT_API_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"


def _fetch_stats(yt_video_ids: list[str]) -> dict[str, dict]:
    """
    Batch-fetch stats. The API supports comma-separated ids up to ~50
    per request — efficient even when we have many videos.
    Returns: {video_id: {views, likes, comments, favorites}}
    """
    if not yt_video_ids or not YOUTUBE_API_KEY:
        return {}
    out: dict[str, dict] = {}
    # Chunk into 50-id batches.
    for i in range(0, len(yt_video_ids), 50):
        batch = yt_video_ids[i:i + 50]
        try:
            r = requests.get(
                YT_API_VIDEOS,
                params={"part": "statistics", "id": ",".join(batch),
                        "key": YOUTUBE_API_KEY},
                timeout=15,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            state.log("WARN", "monitor", f"YouTube stats fetch failed: {e}")
            continue
        for item in r.json().get("items", []):
            stats = item.get("statistics", {})
            out[item["id"]] = {
                "views": int(stats.get("viewCount", 0)) if stats.get("viewCount") else None,
                "likes": int(stats.get("likeCount", 0)) if stats.get("likeCount") else None,
                "comments": int(stats.get("commentCount", 0)) if stats.get("commentCount") else None,
                "favorites": int(stats.get("favoriteCount", 0)) if stats.get("favoriteCount") else None,
            }
    return out


def run_monitor() -> int:
    """
    Pull stats for every video uploaded in the last 30 days. Returns
    count of stats rows recorded.
    """
    if not YOUTUBE_API_KEY:
        state.log("INFO", "monitor",
                  "YOUTUBE_API_KEY not set in .env — skipping stats poll")
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with state.connect() as con:
        rows = list(con.execute(
            "SELECT id, youtube_video_id, short_video_id FROM videos "
            "WHERE status IN ('uploaded', 'embedded') AND uploaded_at >= ? "
            "AND (youtube_video_id IS NOT NULL OR short_video_id IS NOT NULL)",
            (cutoff,),
        ))

    # Map: (yt_video_id) -> (our_video_id, kind)
    yt_to_meta: dict[str, tuple[int, str]] = {}
    for r in rows:
        if r["youtube_video_id"]:
            yt_to_meta[r["youtube_video_id"]] = (r["id"], "long")
        if r["short_video_id"]:
            yt_to_meta[r["short_video_id"]] = (r["id"], "short")

    if not yt_to_meta:
        return 0

    stats = _fetch_stats(list(yt_to_meta.keys()))
    recorded = 0
    for yt_id, s in stats.items():
        our_id, kind = yt_to_meta[yt_id]
        state.record_video_stats(
            video_id=our_id, yt_video_id=yt_id, kind=kind,
            views=s["views"], likes=s["likes"],
            comments=s["comments"], favorites=s["favorites"],
        )
        recorded += 1
    state.log("INFO", "monitor",
              f"recorded stats for {recorded} video(s) "
              f"({len(yt_to_meta) - recorded} not yet indexed by YouTube)")
    return recorded


def should_run_monitor() -> bool:
    """Daily throttle: only poll if 22+ hours since last run."""
    last = state.last_monitor_run_at()
    if not last:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() / 3600 >= 22


if __name__ == "__main__":
    state.init_db()
    n = run_monitor()
    print(f"recorded {n} stat rows")
