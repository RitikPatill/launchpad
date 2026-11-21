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


def _parse_stats(item: dict) -> dict:
    stats = item.get("statistics", {})
    def num(k):
        return int(stats[k]) if stats.get(k) not in (None, "") else None
    return {"views": num("viewCount"), "likes": num("likeCount"),
            "comments": num("commentCount"), "favorites": num("favoriteCount")}


def _fetch_stats_oauth(yt_video_ids: list[str]) -> dict[str, dict]:
    """Same stats via the channel's existing OAuth login (youtube.force-ssl
    scope) - no separate API key needed."""
    from googleapiclient.discovery import build
    from uploader import _load_creds
    try:
        yt = build("youtube", "v3", credentials=_load_creds(), cache_discovery=False)
    except Exception as e:
        state.log("WARN", "monitor", f"OAuth stats client failed: {e}")
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(yt_video_ids), 50):
        batch = yt_video_ids[i:i + 50]
        try:
            resp = yt.videos().list(part="statistics", id=",".join(batch)).execute()
        except Exception as e:
            state.log("WARN", "monitor", f"YouTube stats fetch failed: {e}")
            continue
        for item in resp.get("items", []):
            out[item["id"]] = _parse_stats(item)
    return out


def _fetch_stats(yt_video_ids: list[str]) -> dict[str, dict]:
    """
    Batch-fetch stats. The API supports comma-separated ids up to ~50
    per request — efficient even when we have many videos.
    Returns: {video_id: {views, likes, comments, favorites}}
    """
    if not yt_video_ids:
        return {}
    if not YOUTUBE_API_KEY:
        return _fetch_stats_oauth(yt_video_ids)
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


def _collect_uploaded_videos(days: int = 30) -> tuple[
    dict[str, tuple[int, str]], dict[str, str]
]:
    """Returns (yt_to_meta, yt_to_uploaded_at) for uploads in the last `days`.

    yt_to_meta:        {yt_video_id: (our_video_id, "long"|"short")}
    yt_to_uploaded_at: {yt_video_id: ISO8601 timestamp}
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with state.connect() as con:
        rows = list(con.execute(
            "SELECT id, youtube_video_id, short_video_id, uploaded_at "
            "FROM videos "
            "WHERE status IN ('uploaded', 'embedded') AND uploaded_at >= ? "
            "AND (youtube_video_id IS NOT NULL OR short_video_id IS NOT NULL)",
            (cutoff,),
        ))
    yt_to_meta: dict[str, tuple[int, str]] = {}
    yt_to_uploaded_at: dict[str, str] = {}
    for r in rows:
        if r["youtube_video_id"]:
            yt_to_meta[r["youtube_video_id"]] = (r["id"], "long")
            yt_to_uploaded_at[r["youtube_video_id"]] = r["uploaded_at"] or ""
        if r["short_video_id"]:
            yt_to_meta[r["short_video_id"]] = (r["id"], "short")
            yt_to_uploaded_at[r["short_video_id"]] = r["uploaded_at"] or ""
    return yt_to_meta, yt_to_uploaded_at


def _run_basic_stats(yt_to_meta: dict[str, tuple[int, str]]) -> int:
    """Basic stats via API key (views/likes/comments). Skips silently if
    YOUTUBE_API_KEY isn't set — analytics path can still run."""
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
              f"recorded basic stats for {recorded} video(s) "
              f"({len(yt_to_meta) - recorded} not yet indexed by YouTube)")
    return recorded


def _run_analytics(yt_to_meta: dict[str, tuple[int, str]],
                   yt_to_uploaded_at: dict[str, str]) -> int:
    """Retention/CTR via the YouTube Analytics API (OAuth-based, no API key
    needed). Safe-fails if the OAuth token lacks yt-analytics.readonly —
    youtube_analytics.fetch_video_analytics returns None and we skip the
    insert."""
    try:
        import youtube_analytics
    except Exception as e:
        state.log("WARN", "monitor", f"analytics module load failed: {e}")
        return 0
    if not yt_to_meta:
        return 0
    recorded = 0
    now = datetime.now(timezone.utc)
    for yt_id, (our_id, kind) in yt_to_meta.items():
        # age_hours: how old is this video at the moment of measurement.
        # Used as a comparison axis when meta-improver compares retention
        # across videos at equivalent maturity (24h/72h/7d/30d buckets).
        try:
            uploaded_iso = yt_to_uploaded_at.get(yt_id) or ""
            uploaded_dt = datetime.fromisoformat(uploaded_iso) if uploaded_iso else now
            age_hours = max(0, int((now - uploaded_dt).total_seconds() // 3600))
        except Exception:
            age_hours = 0
        metrics = youtube_analytics.fetch_video_analytics(yt_id)
        if metrics is None:
            # Either scope missing (one-line nudge already logged) or
            # a transient error. Skip; basic stats still recorded above.
            continue
        try:
            state.record_video_analytics(
                video_id=our_id, yt_video_id=yt_id, kind=kind,
                age_hours=age_hours, metrics=metrics,
            )
            recorded += 1
        except Exception as e:
            state.log("WARN", "monitor",
                      f"analytics record failed for {yt_id}: {e}")
    if recorded:
        state.log("INFO", "monitor",
                  f"recorded analytics for {recorded} video(s)")
    return recorded


def run_monitor() -> int:
    """
    Pull both basic stats AND retention/CTR analytics for every video
    uploaded in the last 30 days. Each path is independent — if one
    fails (missing API key, missing OAuth scope, transient API error),
    the other still runs.

    Returns count of basic-stats rows recorded (back-compat with callers).
    """
    # Basic stats are cheap (1 quota unit per 50 videos): track the WHOLE
    # back catalogue so the meta-improver learns from every upload.
    all_meta, _ = _collect_uploaded_videos(days=3650)
    basic = _run_basic_stats(all_meta)
    # Retention analytics are one query per video: recent uploads only.
    yt_to_meta, yt_to_uploaded_at = _collect_uploaded_videos(days=30)
    try:
        _run_analytics(yt_to_meta, yt_to_uploaded_at)
    except Exception as e:
        state.log("WARN", "monitor", f"analytics path raised unexpectedly: {e}")
    state.log("INFO", "monitor", f"monitor run complete ({basic} stat rows)")
    return basic


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
