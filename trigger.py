"""
Discovery trigger. Polls autodev's and agent-radar's SQLite DBs (read-only)
for projects that have transitioned to status='done' and haven't yet been
queued for a video.

We deliberately use a separate, read-only sqlite connection per sibling
DB so launchpad can't accidentally mutate state in autodev or agent-radar.
"""
from __future__ import annotations

import sqlite3

import state
from config import AGENT_RADAR_DB, AUTODEV_DB


def _scan_db(db_path, source_label: str) -> int:
    """Pull all status=done projects from a sibling DB. Returns count newly queued."""
    if not db_path.exists():
        return 0
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    queued = 0
    try:
        rows = con.execute(
            "SELECT slug, title, description, repo_url FROM projects "
            "WHERE status = 'done' AND repo_url IS NOT NULL"
        ).fetchall()
    except sqlite3.Error as e:
        state.log("WARN", "trigger", f"{source_label} scan failed: {e}")
        con.close()
        return 0
    con.close()

    for r in rows:
        if state.has_video_for(source_label, r["slug"]):
            continue
        # Pages URL: derived from the repo's homepage if Pages is enabled.
        # We let the recorder decide whether to use repo page or pages URL.
        pages_url = None  # we'll resolve at recording time via GitHub API
        new_id = state.queue_video(
            source_orchestrator=source_label,
            project_slug=r["slug"],
            project_title=r["title"],
            project_description=r["description"] or "",
            project_repo_url=r["repo_url"],
            project_pages_url=pages_url,
        )
        if new_id:
            queued += 1
            state.log("INFO", "trigger",
                      f"queued video #{new_id}: {source_label}/{r['slug']}")
    return queued


def scan_all_siblings() -> int:
    total = 0
    total += _scan_db(AUTODEV_DB, "autodev")
    total += _scan_db(AGENT_RADAR_DB, "agent-radar")
    return total
