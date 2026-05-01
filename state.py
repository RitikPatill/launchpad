"""
SQLite state for launchpad.

Tracks one row per video we attempt to make. Status state machine:

  queued      -> a sibling project completed and we noted it
  scripting   -> screenplay agent is writing the voice-over script
  recording   -> Playwright is capturing the screen
  rendering   -> ffmpeg is muxing video + audio
  uploading   -> YouTube Data API upload in progress
  uploaded    -> live on YouTube (terminal success)
  embedded    -> link added to the source repo's README
  failed_terminal -> after MAX_VIDEO_RETRIES
"""
from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_orchestrator TEXT NOT NULL,        -- 'autodev' | 'agent-radar'
    project_slug       TEXT NOT NULL,
    project_title      TEXT,
    project_description TEXT,
    project_repo_url   TEXT,
    project_pages_url  TEXT,
    status             TEXT NOT NULL,
    script             TEXT,
    title              TEXT,
    description        TEXT,
    tags               TEXT,
    video_path         TEXT,
    audio_path         TEXT,
    final_path         TEXT,
    youtube_video_id   TEXT,
    youtube_url        TEXT,
    thumbnail_path     TEXT,
    srt_path           TEXT,
    duration_s         REAL,
    attempt_count      INTEGER DEFAULT 0,
    last_error         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    uploaded_at        TEXT,
    UNIQUE(source_orchestrator, project_slug)
);

CREATE TABLE IF NOT EXISTS claude_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    component   TEXT NOT NULL,
    model       TEXT,
    duration_s  REAL,
    ok          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL,
    component   TEXT NOT NULL,
    message     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
    finally:
        con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)


def log(level: str, component: str, message: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO log(ts, level, component, message) VALUES (?, ?, ?, ?)",
            (_now(), level, component, message),
        )
    print(f"[{level}] {component}: {message}", flush=True)


# --- Videos -------------------------------------------------------------- #

def queue_video(*, source_orchestrator: str, project_slug: str,
                project_title: str, project_description: str,
                project_repo_url: str, project_pages_url: str | None) -> int | None:
    """Add a project to the video queue. Returns the new id or None if dup."""
    try:
        with connect() as con:
            cur = con.execute(
                "INSERT INTO videos(source_orchestrator, project_slug, project_title, "
                "project_description, project_repo_url, project_pages_url, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (source_orchestrator, project_slug, project_title,
                 project_description, project_repo_url, project_pages_url,
                 _now(), _now()),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # already queued


def update_video(video_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE videos SET {cols} WHERE id = ?",
                    (*fields.values(), video_id))


def get_video(video_id: int) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()


def next_video_in_status(status: str) -> sqlite3.Row | None:
    with connect() as con:
        return con.execute(
            "SELECT * FROM videos WHERE status = ? ORDER BY created_at LIMIT 1",
            (status,),
        ).fetchone()


def has_video_for(source_orchestrator: str, project_slug: str) -> bool:
    with connect() as con:
        row = con.execute(
            "SELECT 1 FROM videos WHERE source_orchestrator = ? AND project_slug = ?",
            (source_orchestrator, project_slug),
        ).fetchone()
        return row is not None


def uploads_in_window(hours: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM videos WHERE status IN ('uploaded','embedded') "
            "AND uploaded_at >= ?", (cutoff,),
        ).fetchone()
        return row["n"]


def last_uploaded_at() -> datetime | None:
    with connect() as con:
        row = con.execute(
            "SELECT MAX(uploaded_at) AS ts FROM videos WHERE status IN ('uploaded','embedded')"
        ).fetchone()
    if row and row["ts"]:
        return datetime.fromisoformat(row["ts"])
    return None


# --- Claude rate-limit --------------------------------------------------- #

def record_claude_call(component: str, model: str, duration_s: float, ok: bool) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO claude_calls(ts, component, model, duration_s, ok) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), component, model, duration_s, 1 if ok else 0),
        )


def claude_calls_in_window(hours: int = 5) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM claude_calls WHERE ts >= ?", (cutoff,),
        ).fetchone()
        return row["n"]
