"""
Extra Shorts: the reach multiplier.

Shorts bring ~75% of the channel's views, yet each project used to get one.
Every finished motion video now gets NEW_VIDEO_ANGLES more Shorts, and the
back catalogue (older projects, best performers first) gets fresh
motion-style Shorts too. Runs as its own tiny state machine, one step per
tick, with its own pacing, so it can neither block nor be starved by the
main pipeline.

  queued -> (storyboard + render) -> uploading -> uploaded

Robustness rules (from the pre-launch review):
- renders live in RENDER_DIR/extra/x-<id>/, outside the per-video dirs that
  _cleanup_old_renders deletes, and are removed after a successful upload
- record-first: the YouTube id is stored the moment the upload returns, so a
  later error (captions, crash) can never upload the same Short twice
- failures back off exponentially (30 min, 1 h, 2 h ...) instead of burning
  attempts 10 minutes apart; a finished storyboard is never thrown away
- pacing: in the upload window, <= EXTRA_SHORTS_PER_DAY, >= 2 h after a
  long-form upload and >= 3 h after any other Short
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import alerts
import motion_pipeline
import state
import uploader
from config import (
    EXTRA_SHORTS_BACKCATALOG, EXTRA_SHORTS_ENABLED, EXTRA_SHORTS_PER_DAY, RENDER_DIR,
    WEEKDAY_UPLOAD_HOURS, WEEKEND_UPLOAD_HOURS,
)

NEW_VIDEO_ANGLES = (
    "the clever trick inside the code - show the exact lines that make it work and why they matter",
    "the architecture in one picture - how the pieces connect, told as a story of one request flowing through",
)
BACKCATALOG_ANGLE = "the single most surprising thing this project does, for viewers who have never seen it"
MAX_ATTEMPTS = 6
SHORT_GAP_HOURS = 3
LONG_GAP_HOURS = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS extra_shorts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      INTEGER NOT NULL,
    angle         TEXT NOT NULL,
    status        TEXT NOT NULL,
    dir           TEXT,
    short_path    TEXT,
    yt_video_id   TEXT,
    yt_url        TEXT,
    attempt_count INTEGER DEFAULT 0,
    next_try_at   TEXT,
    revive_count  INTEGER DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    uploaded_at   TEXT,
    UNIQUE(video_id, angle)
)
"""
_MIGRATIONS = ("ALTER TABLE extra_shorts ADD COLUMN next_try_at TEXT",
               "ALTER TABLE extra_shorts ADD COLUMN revive_count INTEGER DEFAULT 0")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def init() -> None:
    import sqlite3
    with state.connect() as con:
        con.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass


def _enqueue(video_id: int, angle: str) -> None:
    ts = _now().isoformat()
    with state.connect() as con:
        con.execute("INSERT OR IGNORE INTO extra_shorts(video_id, angle, status, created_at, updated_at) "
                    "VALUES (?, ?, 'queued', ?, ?)", (video_id, angle, ts, ts))


def _update(row_id: int, **fields) -> None:
    fields["updated_at"] = _now().isoformat()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with state.connect() as con:
        con.execute(f"UPDATE extra_shorts SET {cols} WHERE id = ?", (*fields.values(), row_id))


def _is_motion(script: str | None) -> bool:
    try:
        return json.loads(script or "{}").get("engine") == "motion"
    except (json.JSONDecodeError, TypeError):
        return False


def plan() -> None:
    """Queue angles for finished motion videos, and top up the back catalogue."""
    with state.connect() as con:
        done = list(con.execute(
            "SELECT id, script FROM videos WHERE status = 'embedded' AND youtube_video_id IS NOT NULL"))
    motion_ids = {r["id"] for r in done if _is_motion(r["script"])}
    for vid in motion_ids:
        for a in NEW_VIDEO_ANGLES:
            _enqueue(vid, a)
    if not EXTRA_SHORTS_BACKCATALOG:
        return
    with state.connect() as con:      # read AFTER the inserts above
        pending = con.execute("SELECT COUNT(*) FROM extra_shorts WHERE status IN ('queued', 'uploading')").fetchone()[0]
        have = {r[0] for r in con.execute("SELECT DISTINCT video_id FROM extra_shorts")}
        ranked = con.execute(
            """SELECT v.id FROM videos v
               LEFT JOIN (SELECT video_id, MAX(views) AS views FROM video_stats GROUP BY video_id) s ON s.video_id = v.id
               WHERE v.status = 'embedded' AND v.project_repo_url IS NOT NULL AND v.youtube_video_id IS NOT NULL
               ORDER BY COALESCE(s.views, 0) DESC, v.id DESC""").fetchall()
    if pending >= 2:
        return
    for (vid,) in ranked:
        if vid not in have and vid not in motion_ids:
            _enqueue(vid, BACKCATALOG_ANGLE)
            break


def _revive() -> None:
    """Failed rows get one more full set of attempts after 3 days (outages pass)."""
    cutoff = (_now() - timedelta(days=3)).isoformat()
    with state.connect() as con:
        rows = list(con.execute("SELECT id, dir FROM extra_shorts WHERE status = 'failed_terminal' "
                                "AND COALESCE(revive_count, 0) < 1 AND updated_at < ?", (cutoff,)))
    for r in rows:
        status = "uploading" if r["dir"] and (Path(r["dir"]) / "shorts_final.mp4").exists() else "queued"
        _update(r["id"], status=status, attempt_count=0, next_try_at=None, revive_count=1)


def _in_window() -> bool:
    now = datetime.now().astimezone()
    lo, hi = WEEKEND_UPLOAD_HOURS if now.weekday() >= 5 else WEEKDAY_UPLOAD_HOURS
    return lo <= now.hour < hi


def _can_upload() -> tuple[bool, str]:
    if not _in_window():
        return False, "outside upload window"
    cutoff = (_now() - timedelta(hours=24)).isoformat()
    with state.connect() as con:
        n = con.execute("SELECT COUNT(*) FROM extra_shorts WHERE uploaded_at >= ?", (cutoff,)).fetchone()[0]
    if n >= EXTRA_SHORTS_PER_DAY:
        return False, "daily extra-Shorts cap"
    last_long = state.last_uploaded_at()
    if last_long and _now() - last_long < timedelta(hours=LONG_GAP_HOURS):
        return False, "too close to a long-form upload"
    last_short = state.last_short_uploaded_at()
    if last_short and _now() - last_short < timedelta(hours=SHORT_GAP_HOURS):
        return False, "too close to another Short"
    return True, ""


def _fail(row, v, e: Exception) -> None:
    n = (row["attempt_count"] or 0) + 1
    terminal = n >= MAX_ATTEMPTS
    backoff = timedelta(minutes=30 * (2 ** (n - 1)))
    _update(row["id"], attempt_count=n, last_error=str(e)[:500],
            next_try_at=None if terminal else (_now() + backoff).isoformat(),
            status="failed_terminal" if terminal else row["status"])
    state.log("ERROR", "extra_shorts",
              f"extra Short {row['id']} (video #{v['id'] if v else '?'}) failed ({n}/{MAX_ATTEMPTS}), "
              f"{'giving up for now' if terminal else 'retry in ' + str(backoff)}: {str(e)[:300]}")
    if terminal:
        alerts.notify("extra-short-failed", "Extra Short gave up (will revive in 3 days)",
                      f"video #{v['id'] if v else '?'}: {str(e)[:200]}", cooldown_hours=24)


def step() -> bool:
    """Advance one extra Short by one step. Returns True if work was done."""
    if not EXTRA_SHORTS_ENABLED:
        return False
    init()
    _revive()
    plan()
    now = _now().isoformat()
    with state.connect() as con:
        rows = list(con.execute(
            "SELECT * FROM extra_shorts WHERE status IN ('queued', 'uploading') "
            "AND (next_try_at IS NULL OR next_try_at <= ?) "
            "ORDER BY CASE status WHEN 'uploading' THEN 0 ELSE 1 END, id", (now,)))
    if not rows:
        return False
    with state.connect() as con:
        # Rendered-but-not-uploaded rows, INCLUDING ones waiting in backoff.
        backlog = con.execute("SELECT COUNT(*) FROM extra_shorts WHERE status = 'uploading' "
                              "AND yt_video_id IS NULL").fetchone()[0]
        in_backoff = con.execute("SELECT COUNT(*) FROM extra_shorts WHERE status = 'uploading' "
                                 "AND next_try_at IS NOT NULL AND next_try_at > ?", (now,)).fetchone()[0]
    row = rows[0]
    if row["status"] == "uploading":
        ok, _why = _can_upload()
        if not ok:
            row = next((r for r in rows if r["status"] == "queued"), None)   # render ahead while waiting
            if row is None:
                return False
    if row["status"] == "queued" and (backlog >= 2 or in_backoff):
        return False   # circuit breaker: never pile up renders during an upload outage
    v = state.get_video(row["video_id"])
    if not v:
        _update(row["id"], status="failed_terminal", last_error="video row missing")
        return False
    try:
        if row["status"] == "queued":
            out_dir = RENDER_DIR / "extra" / f"x-{row['id']}"
            # Never redo FINISHED creative work (both files present; script_short
            # writes the metadata first and the script last).
            if not ((out_dir / "shorts_script.json").exists() and (out_dir / "shorts_metadata.json").exists()):
                ws = motion_pipeline.sibling_workspace(v["source_orchestrator"], v["project_slug"])
                motion_pipeline.script_short(v, ws, out_dir, angle=row["angle"])
            final, _srt = motion_pipeline.render_short(v, out_dir)
            _update(row["id"], status="uploading", dir=str(out_dir), short_path=str(final),
                    attempt_count=0, next_try_at=None)
            return True
        # uploading
        out_dir = Path(row["dir"])
        if not row["yt_video_id"]:
            md = json.loads((out_dir / "shorts_metadata.json").read_text(encoding="utf-8"))
            de = md.get("de") or {}
            res = uploader.upload(
                video_path=Path(row["short_path"]), title=md["title"], description=md["description"],
                tags=(md.get("tags") or [])[:15],
                localizations=({"de": {"title": de["title"][:100], "description": de.get("description", "")[:4900]}}
                               if de.get("title") else None))
            _update(row["id"], status="uploaded", yt_video_id=res["video_id"], yt_url=res["url"],
                    uploaded_at=_now().isoformat())
            state.log("INFO", "extra_shorts", f"uploaded extra Short for #{v['id']}: {res['url']}")
            yt_id = res["video_id"]
        else:
            yt_id = row["yt_video_id"]
            _update(row["id"], status="uploaded")
        srt = out_dir / "shorts_captions.srt"
        if srt.exists():
            try:
                uploader.upload_caption(video_id=yt_id, srt_path=srt)
            except Exception as e:
                state.log("WARN", "extra_shorts", f"captions skipped for {yt_id}: {str(e)[:200]}")
        shutil.rmtree(out_dir, ignore_errors=True)
        return True
    except Exception as e:
        from claude_cli import RateLimited
        if isinstance(e, RateLimited):
            state.log("WARN", "extra_shorts", str(e)[:300])
            _update(row["id"], next_try_at=(_now() + timedelta(minutes=45)).isoformat())
            return False
        _fail(row, v, e)
        return False
