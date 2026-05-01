"""
launchpad orchestrator. Drives video creation through:

  queued -> scripting -> recording -> rendering -> uploading -> uploaded -> embedded

Each tick:
  1. Settle: scan sibling DBs for newly-completed projects (trigger.scan).
  2. Drive: pick the oldest video in any non-terminal status, advance it
     by exactly one step (Sonnet call OR Playwright record OR ffmpeg
     compose OR YouTube upload).
  3. Pace gate: only the UPLOADING step is gated by commit-window /
     daily-cap rules. Earlier steps run as fast as resources allow
     because they don't show up publicly.

Same resilience pattern as autodev/agent-radar: SQLite restart-safe,
catch-up window after laptop downtime, retry-with-cap on failures.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil as _shutil
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import claude_cli
import compositor
import recorder
import state
import trigger
import tts
import uploader
from agents import metadata, screenplay, thumbnail
from config import (
    BASE_DIR,
    GITHUB_TOKEN,
    MAX_UPLOADS_PER_WEEK,
    MAX_VIDEO_RETRIES,
    MIN_HOURS_BETWEEN_UPLOADS,
    ORCHESTRATOR_TICK_SECONDS,
    RENDER_DIR,
    WEEKDAY_UPLOAD_HOURS,
    WEEKEND_UPLOAD_HOURS,
    YT_TOKEN_PATH,
)


# --- Pacing helpers ------------------------------------------------------ #

def _now_local() -> datetime:
    return datetime.now().astimezone()


def _is_upload_window() -> bool:
    now = _now_local()
    weekend = now.weekday() >= 5
    lo, hi = WEEKEND_UPLOAD_HOURS if weekend else WEEKDAY_UPLOAD_HOURS
    return lo <= now.hour < hi


def _can_upload_now() -> tuple[bool, str]:
    if not _is_upload_window():
        return False, "outside upload window"
    if state.uploads_in_window(hours=24 * 7) >= MAX_UPLOADS_PER_WEEK:
        return False, f"weekly upload cap ({MAX_UPLOADS_PER_WEEK})"
    last = state.last_uploaded_at()
    if last:
        gap_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if gap_h < MIN_HOURS_BETWEEN_UPLOADS:
            return False, f"min gap {gap_h:.1f}h/{MIN_HOURS_BETWEEN_UPLOADS}h"
    return True, ""


# --- Step drivers -------------------------------------------------------- #

def _step_scripting(v) -> None:
    sp = screenplay.write_screenplay(
        title=v["project_title"],
        description=v["project_description"],
        repo_url=v["project_repo_url"],
        pages_url=v["project_pages_url"],
    )
    md = metadata.write_metadata(
        title=v["project_title"],
        description=v["project_description"],
        repo_url=v["project_repo_url"],
        script="\n".join(s.get("voice_over", "") for s in sp["scenes"]),
    )
    state.update_video(
        v["id"], status="recording",
        script=json.dumps(sp), title=md["title"],
        description=md["description"],
        tags=json.dumps(md.get("tags", [])),
    )


def _step_recording(v) -> None:
    sp = json.loads(v["script"])
    out_dir = RENDER_DIR / f"video-{v['id']}"
    webm = recorder.record(sp, out_dir)
    state.update_video(v["id"], status="rendering", video_path=str(webm))


def _step_rendering(v) -> None:
    sp = json.loads(v["script"])
    out_dir = RENDER_DIR / f"video-{v['id']}"
    audio_path = out_dir / "narration.mp3"
    srt_path = out_dir / "captions.srt"
    tts.synthesize_screenplay(sp, audio_path, srt_path)
    final_path = out_dir / "final.mp4"
    compositor.compose(
        video_in=Path(v["video_path"]),
        audio_in=audio_path,
        output_path=final_path,
    )
    # Generate the thumbnail from a key frame of the recorded video.
    thumb_path = out_dir / "thumbnail.jpg"
    try:
        thumbnail.compose_thumbnail(
            video_path=Path(v["video_path"]),
            title=v["title"], handle="@RitikPatill",
            output_path=thumb_path,
        )
    except Exception as e:
        state.log("WARN", "orchestrator", f"thumbnail compose failed: {e}")
        thumb_path = None  # YouTube will use first frame as fallback

    state.update_video(
        v["id"], status="uploading",
        audio_path=str(audio_path),
        final_path=str(final_path),
        srt_path=str(srt_path) if srt_path.exists() else None,
        thumbnail_path=str(thumb_path) if thumb_path else None,
    )


def _step_uploading(v) -> None:
    tags = json.loads(v["tags"]) if v["tags"] else []
    result = uploader.upload(
        video_path=Path(v["final_path"]),
        title=v["title"], description=v["description"], tags=tags,
    )
    video_id = result["video_id"]
    # Custom thumbnail (best-effort — needs verified channel).
    if v["thumbnail_path"]:
        try:
            uploader.upload_thumbnail(
                video_id=video_id,
                thumbnail_path=Path(v["thumbnail_path"]),
            )
        except Exception as e:
            state.log("WARN", "orchestrator", f"thumbnail upload skipped: {e}")
    # Caption track in English (skip if SRT missing).
    if v["srt_path"]:
        try:
            uploader.upload_caption(
                video_id=video_id, srt_path=Path(v["srt_path"]),
            )
        except Exception as e:
            state.log("WARN", "orchestrator", f"caption upload skipped: {e}")
    state.update_video(
        v["id"], status="uploaded",
        youtube_video_id=video_id,
        youtube_url=result["url"],
        uploaded_at=datetime.now(timezone.utc).isoformat(),
    )


def _cleanup_old_renders() -> None:
    """Delete render dirs for videos uploaded more than 14 days ago.
    Keeps disk usage bounded over a year of operation."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    with state.connect() as con:
        rows = list(con.execute(
            "SELECT id, uploaded_at FROM videos WHERE status IN ('uploaded','embedded') "
            "AND uploaded_at IS NOT NULL"
        ))
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["uploaded_at"])
        except Exception:
            continue
        if ts < cutoff:
            d = RENDER_DIR / f"video-{r['id']}"
            if d.exists():
                import shutil
                try:
                    shutil.rmtree(d)
                    state.log("INFO", "cleanup", f"removed render dir for video #{r['id']}")
                except Exception:
                    pass


# --- Tick ---------------------------------------------------------------- #

_TICK_COUNT = 0


def tick() -> None:
    global _TICK_COUNT
    _TICK_COUNT += 1

    # Phase 0 — periodic cleanup (every ~24h at our 30-min cadence).
    if _TICK_COUNT % 48 == 0:
        _cleanup_old_renders()

    # Phase 1 — discover newly-completed sibling projects.
    trigger.scan_all_siblings()

    # Phase 2 — drive the oldest in-flight video forward.
    for status_to_drive in ("queued", "scripting", "recording", "rendering", "uploading"):
        v = state.next_video_in_status(status_to_drive)
        if not v:
            continue

        # Mark the kickoff transition for "queued" so we don't pick it
        # twice across overlapping ticks.
        if status_to_drive == "queued":
            state.update_video(v["id"], status="scripting")
            v = state.get_video(v["id"])
            status_to_drive = "scripting"

        # Upload step is the only one gated by pacing rules.
        if status_to_drive == "uploading":
            ok, reason = _can_upload_now()
            if not ok:
                state.log("INFO", "orchestrator", f"video #{v['id']} ready, holding upload — {reason}")
                return

        try:
            if status_to_drive == "scripting":
                _step_scripting(v)
            elif status_to_drive == "recording":
                _step_recording(v)
            elif status_to_drive == "rendering":
                _step_rendering(v)
            elif status_to_drive == "uploading":
                _step_uploading(v)
        except claude_cli.RateLimited as e:
            state.log("WARN", "orchestrator", str(e))
        except Exception as e:
            attempts = (v["attempt_count"] or 0) + 1
            new_status = "failed_terminal" if attempts >= MAX_VIDEO_RETRIES else status_to_drive
            state.log("ERROR", "orchestrator",
                      f"video #{v['id']} {status_to_drive} failed (attempt {attempts}): {e}")
            state.update_video(v["id"], status=new_status,
                               attempt_count=attempts, last_error=str(e)[:500])
        return  # one step per tick


# --- Main loop ----------------------------------------------------------- #

def _self_heal_on_startup() -> None:
    with state.connect() as con:
        rows = list(con.execute(
            "SELECT id, status, project_slug FROM videos WHERE status NOT IN "
            "('uploaded', 'embedded', 'failed_terminal')"
        ))
    for r in rows:
        state.log("INFO", "orchestrator",
                  f"resuming video #{r['id']} ({r['project_slug']}) at status={r['status']}")


def _preflight_ok(*, require_yt: bool = False) -> bool:
    if not GITHUB_TOKEN:
        state.log("ERROR", "orchestrator", "GITHUB_TOKEN missing in .env")
        return False
    if not _shutil.which("claude"):
        state.log("ERROR", "orchestrator", "`claude` CLI not on PATH")
        return False
    try:
        import edge_tts  # noqa: F401
        import imageio_ffmpeg  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as e:
        state.log("ERROR", "orchestrator", f"missing python deps: {e}")
        return False
    if require_yt and not YT_TOKEN_PATH.exists():
        state.log("ERROR", "orchestrator",
                  f"YouTube token not found at {YT_TOKEN_PATH}; run `python orchestrator.py auth`")
        return False
    return True


def run_forever() -> None:
    state.init_db()
    state.log("INFO", "orchestrator", f"launchpad starting (pid={os.getpid()})")
    if not _preflight_ok():
        sys.exit(1)
    _self_heal_on_startup()
    while True:
        try:
            tick()
        except Exception:
            state.log("ERROR", "orchestrator",
                      f"unhandled tick error:\n{traceback.format_exc()}")
        sleep_for = ORCHESTRATOR_TICK_SECONDS + random.randint(-60, 60)
        time.sleep(max(60, sleep_for))


# --- CLI ----------------------------------------------------------------- #

def _print_status() -> None:
    with state.connect() as con:
        counts = dict(con.execute(
            "SELECT status, COUNT(*) AS n FROM videos GROUP BY status"
        ).fetchall())
    print("\nvideo pipeline status:")
    for s in ("queued", "scripting", "recording", "rendering", "uploading",
              "uploaded", "embedded", "failed_terminal"):
        print(f"  {s:18s} {counts.get(s, 0)}")
    print(f"\nuploads in last 7 days: {state.uploads_in_window(168)}/{MAX_UPLOADS_PER_WEEK}")
    print(f"claude calls in last 5h: {state.claude_calls_in_window(5)}")


def _print_health() -> None:
    print("\n=== launchpad health ===\n")
    print(f"YouTube auth:     {'YES' if YT_TOKEN_PATH.exists() else 'NO — run `python orchestrator.py auth`'}")
    _print_status()


def main() -> None:
    parser = argparse.ArgumentParser(description="launchpad — autonomous YouTube agent")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "tick", "scan", "auth", "preflight",
                                 "status", "health"])
    args = parser.parse_args()
    state.init_db()

    if args.command == "run":
        run_forever()
    elif args.command == "tick":
        tick()
    elif args.command == "scan":
        n = trigger.scan_all_siblings()
        print(f"queued {n} new videos")
    elif args.command == "auth":
        uploader.authenticate_browser_flow()
    elif args.command == "preflight":
        sys.exit(0 if _preflight_ok(require_yt=True) else 1)
    elif args.command == "status":
        _print_status()
    elif args.command == "health":
        _print_health()


if __name__ == "__main__":
    main()
