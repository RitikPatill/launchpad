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
import deployer
import recorder
import state
import trigger
import tts
import uploader
from agents import editor, metadata, meta_improver, screenplay, thumbnail
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

def _get_sibling_workspace(source_orchestrator: str, project_slug: str) -> Path | None:
    """Look up the project's local clone path in the sibling orchestrator's DB."""
    import sqlite3
    from config import AGENT_RADAR_DB, AUTODEV_DB
    db = {"autodev": AUTODEV_DB, "agent-radar": AGENT_RADAR_DB}.get(source_orchestrator)
    if not db or not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT workspace_path FROM projects WHERE slug = ?", (project_slug,),
        ).fetchone()
    finally:
        con.close()
    return Path(row["workspace_path"]) if row and row["workspace_path"] else None


def _step_deploying(v) -> None:
    """
    Try to auto-deploy the project to HuggingFace Spaces so the video
    can show a real live demo. If the project isn't deployable
    (Streamlit/Gradio/static), or HF auth missing, advance to scripting
    anyway with no demo URL — the recorder will fall back to repo
    walkthrough.
    """
    workspace = _get_sibling_workspace(v["source_orchestrator"], v["project_slug"])
    if workspace is None:
        # manual_test or workspace missing — skip deploy gracefully.
        state.update_video(v["id"], status="scripting")
        return

    deploy_url: str | None = None
    try:
        deploy_url = deployer.deploy_project(
            workspace=workspace,
            slug=v["project_slug"],
            project_title=v["project_title"],
            project_description=v["project_description"],
        )
    except deployer.DeployError as e:
        state.log("WARN", "orchestrator", f"HF deploy skipped: {e}")
    except Exception as e:
        state.log("WARN", "orchestrator", f"HF deploy errored: {e}")

    fields: dict[str, object] = {"status": "scripting"}
    if deploy_url:
        fields["deploy_url"] = deploy_url
        fields["project_pages_url"] = deploy_url
    state.update_video(v["id"], **fields)


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
    raw_final = out_dir / "raw_final.mp4"
    compositor.compose(
        video_in=Path(v["video_path"]),
        audio_in=audio_path,
        output_path=raw_final,
    )
    # Polish: intro card + outro card + burned-in captions.
    final_path = out_dir / "final.mp4"
    try:
        editor.polish_video(
            main_video=raw_final,
            srt=srt_path if srt_path.exists() else None,
            project_title=v["project_title"] or v["title"],
            handle="@RitikPatill",
            output_path=final_path,
        )
    except Exception as e:
        state.log("WARN", "orchestrator", f"polish failed, shipping raw: {e}")
        import shutil
        shutil.copyfile(raw_final, final_path)
    # Generate the thumbnail from a key frame of the RAW recording (before
    # the intro card was prepended) so we get a real product shot.
    thumb_path = out_dir / "thumbnail.jpg"
    try:
        thumbnail.compose_thumbnail(
            video_path=Path(v["video_path"]),
            title=v["title"], handle="@RitikPatill",
            output_path=thumb_path,
        )
    except Exception as e:
        state.log("WARN", "orchestrator", f"thumbnail compose failed: {e}")
        thumb_path = None

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
        v["id"], status="shorting",
        youtube_video_id=video_id,
        youtube_url=result["url"],
        uploaded_at=datetime.now(timezone.utc).isoformat(),
    )


def _step_shorting(v) -> None:
    """
    Generate a YouTube Shorts variant by cropping/trimming the long-form
    video, then upload as a separate video tagged #Shorts. If anything
    fails, mark the main video 'uploaded' anyway — Shorts are a bonus,
    not a requirement.
    """
    final_path = Path(v["final_path"])
    short_path = final_path.parent / "short.mp4"
    try:
        editor.make_short(source_long_video=final_path, output_path=short_path)
    except Exception as e:
        state.log("WARN", "orchestrator", f"shorts gen failed: {e}")
        state.update_video(v["id"], status="uploaded")
        return

    short_title = ((v["title"] or "")[:60] + " #Shorts")[:100]
    short_desc = (
        (v["description"] or "")[:900]
        + "\n\n#Shorts — full walkthrough: " + (v["youtube_url"] or "")
    )
    tags_full = (json.loads(v["tags"]) if v["tags"] else [])[:8] + ["shorts"]

    try:
        result = uploader.upload(
            video_path=short_path,
            title=short_title,
            description=short_desc,
            tags=tags_full,
        )
        state.update_video(
            v["id"], status="uploaded",
            short_path=str(short_path),
            short_video_id=result["video_id"],
            short_url=result["url"],
        )
    except Exception as e:
        state.log("WARN", "orchestrator", f"shorts upload failed (long-form is live): {e}")
        state.update_video(v["id"], status="uploaded", short_path=str(short_path))


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
    for status_to_drive in ("queued", "deploying", "scripting", "recording",
                            "rendering", "uploading", "shorting"):
        v = state.next_video_in_status(status_to_drive)
        if not v:
            continue

        # Mark the kickoff transition for "queued" so we don't pick it
        # twice across overlapping ticks. Goes to deploying first.
        if status_to_drive == "queued":
            state.update_video(v["id"], status="deploying")
            v = state.get_video(v["id"])
            status_to_drive = "deploying"

        # Upload step is gated by pacing rules. Shorting reuses the same
        # gate (don't double-publish in a 5-min window even if main was
        # within the cap).
        if status_to_drive in ("uploading", "shorting"):
            ok, reason = _can_upload_now()
            if not ok:
                state.log("INFO", "orchestrator",
                          f"video #{v['id']} {status_to_drive} held — {reason}")
                return

        try:
            if status_to_drive == "deploying":
                _step_deploying(v)
            elif status_to_drive == "scripting":
                _step_scripting(v)
            elif status_to_drive == "recording":
                _step_recording(v)
            elif status_to_drive == "rendering":
                _step_rendering(v)
            elif status_to_drive == "uploading":
                _step_uploading(v)
            elif status_to_drive == "shorting":
                _step_shorting(v)
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


def _maybe_run_meta() -> None:
    """Run the Opus weekly meta-improver if 144h have passed since last run."""
    last = state.last_meta_run_at()
    if last:
        gap_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if gap_h < 144:  # 6 days
            return
    try:
        meta_improver.run_meta_review()
    except Exception as e:
        state.log("ERROR", "orchestrator", f"meta-improver failed: {e}")


def run_forever() -> None:
    state.init_db()
    state.log("INFO", "orchestrator", f"launchpad starting (pid={os.getpid()})")
    if not _preflight_ok():
        sys.exit(1)
    _self_heal_on_startup()
    while True:
        try:
            tick()
            _maybe_run_meta()
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
                                 "status", "health", "meta", "force_test"])
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
    elif args.command == "meta":
        meta_improver.run_meta_review()
    elif args.command == "force_test":
        # One-off: queue a video for autodev itself (the orchestrator
        # repo) so we can verify pipeline output before any real
        # generated project completes. Bypasses the trigger.
        new_id = state.queue_video(
            source_orchestrator="manual_test",
            project_slug="autodev",
            project_title="autodev",
            project_description="Autonomous multi-agent orchestrator that scouts trending AI/ML topics, picks one, breaks it into milestones, and ships a working GitHub project at human pace via PRs. Self-improving via daily Opus meta-reviewer with eval gating.",
            project_repo_url="https://github.com/RitikPatill/autodev",
            project_pages_url=None,
        )
        if new_id:
            print(f"queued test video #{new_id} for autodev")
        else:
            print("test video already queued earlier")


if __name__ == "__main__":
    main()
