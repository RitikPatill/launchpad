"""YouTube Analytics API integration.

Pulls retention + CTR-style data the basic Data API doesn't expose:
  - averageViewPercentage  (the single best signal for Shorts)
  - averageViewDuration
  - subscribersGained
  - shares, likes, dislikes, comments

Safe-fail design: if the OAuth token doesn't carry the
`yt-analytics.readonly` scope yet (added after the token was minted),
fetch_video_analytics() returns None and logs a one-line nudge. The
rest of the pipeline keeps working unchanged.

To grant the scope, the user runs `python orchestrator.py auth` once.
That re-runs the browser OAuth flow with the full updated scope list
in uploader.SCOPES.
"""
from __future__ import annotations

import state
from uploader import _load_creds


_SCOPE_HINT_LOGGED = False


def _scope_missing_hint() -> None:
    """Log the one-time scope nudge. Idempotent across this process."""
    global _SCOPE_HINT_LOGGED
    if _SCOPE_HINT_LOGGED:
        return
    _SCOPE_HINT_LOGGED = True
    state.log(
        "INFO", "youtube_analytics",
        "yt-analytics.readonly scope not granted on the current OAuth "
        "token. Retention/CTR analytics will be skipped. To enable, run "
        "`python orchestrator.py auth` once to re-grant. The basic stats "
        "loop (views/likes/comments) keeps working regardless.",
    )


def fetch_video_analytics(yt_video_id: str, days: int = 30) -> dict | None:
    """Returns a flat dict of analytics metrics for the given video, or
    None if the scope is missing / API errors. Never raises."""
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except Exception as e:
        state.log("WARN", "youtube_analytics", f"google client import failed: {e}")
        return None

    try:
        creds = _load_creds()
    except Exception as e:
        state.log("WARN", "youtube_analytics", f"creds load failed: {e}")
        return None

    try:
        ya = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    except Exception as e:
        state.log("WARN", "youtube_analytics", f"build client failed: {e}")
        return None

    # Date range: last N days. Analytics API expects YYYY-MM-DD.
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    metrics = ",".join([
        "views",
        "estimatedMinutesWatched",
        "averageViewDuration",
        "averageViewPercentage",
        "subscribersGained",
        "shares",
        "likes",
        "dislikes",
        "comments",
    ])

    try:
        resp = (
            ya.reports()
            .query(
                ids="channel==MINE",
                startDate=start.isoformat(),
                endDate=end.isoformat(),
                metrics=metrics,
                filters=f"video=={yt_video_id}",
            )
            .execute()
        )
    except HttpError as e:
        # 403 with insufficientPermissions / insufficientScope means the
        # current token lacks yt-analytics.readonly. Log a one-line nudge
        # and fail safely.
        msg = str(e).lower()
        if "insufficient" in msg or "scope" in msg or "403" in msg:
            _scope_missing_hint()
            return None
        state.log("WARN", "youtube_analytics",
                  f"analytics fetch HttpError for {yt_video_id}: {e}")
        return None
    except Exception as e:
        state.log("WARN", "youtube_analytics",
                  f"analytics fetch failed for {yt_video_id}: {e}")
        return None

    # Response shape: {"columnHeaders": [...], "rows": [[v1, v2, ...]]}.
    # If a video has zero data in the window (just-uploaded), `rows` is
    # absent — return zeros so callers can store the snapshot anyway.
    rows = resp.get("rows") or []
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    if not rows:
        return {h: 0 for h in headers}
    return dict(zip(headers, rows[0]))
