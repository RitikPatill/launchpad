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
import re
import shutil as _shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nowindow  # noqa: F401  - must be first: hides every child process window
import claude_cli
import compositor
import deployer
import monitor
import recorder
import state
import trigger
import alerts
import extra_shorts
import motion_pipeline
import tts
import uploader
from agents import (
    editor, metadata, meta_improver, screenplay, thumbnail,
    shorts_screenplay, shorts_metadata, shorts_quality,
)
from config import SHORTS_RESOLUTION
from config import (
    BASE_DIR,
    GITHUB_TOKEN,
    HF_DEPLOY_ENABLED,
    MAX_REVIVES,
    MAX_UPLOADS_PER_WEEK,
    MAX_VIDEO_RETRIES,
    REVIVE_AFTER_HOURS,
    STALE_UPLOAD_ALERT_DAYS,
    MIN_HOURS_BETWEEN_UPLOADS,
    MOTION_ENGINE,
    ORCHESTRATOR_TICK_SECONDS,
    RENDER_DIR,
    WEEKDAY_UPLOAD_HOURS,
    WEEKEND_UPLOAD_HOURS,
    YT_TOKEN_PATH,
)


# --- Pacing helpers ------------------------------------------------------ #

def _probe_duration_s(path: Path) -> float | None:
    """ffprobe-style duration probe via the bundled ffmpeg binary. Returns
    seconds or None if probing fails. Never raises — used opportunistically
    to populate videos.duration_s for analytics."""
    try:
        import imageio_ffmpeg
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        # ffmpeg prints duration on stderr in 'Duration: HH:MM:SS.xx' format.
        proc = subprocess.run(
            [ffmpeg_bin, "-i", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", proc.stderr)
        if not m:
            return None
        h, mm, ss = m.groups()
        return int(h) * 3600 + int(mm) * 60 + float(ss)
    except Exception:
        return None


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _is_upload_window() -> bool:
    now = _now_local()
    weekend = now.weekday() >= 5
    lo, hi = WEEKEND_UPLOAD_HOURS if weekend else WEEKDAY_UPLOAD_HOURS
    return lo <= now.hour < hi


SHORT_MIN_GAP_HOURS = 3
ANY_UPLOAD_GAP_HOURS = 1


def _resumable_upload(stage: str):
    """An upload whose YouTube id is already recorded (crash mid-step) is
    finished first and ungated, even if an older row is waiting on pacing."""
    col = {"uploading": "youtube_video_id", "shorts_uploading": "short_video_id"}.get(stage)
    if not col:
        return None
    with state.connect() as con:
        return con.execute(f"SELECT * FROM videos WHERE status = ? AND {col} IS NOT NULL "
                           "ORDER BY created_at LIMIT 1", (stage,)).fetchone()


_TOPIC_STOP = {"the", "and", "for", "with", "your", "you", "its", "that", "this", "what", "why", "how", "when",
               "from", "into", "over", "before", "after", "one", "two", "not", "can", "are", "does", "open",
               "source", "project", "tool", "built", "using", "use", "who", "get", "got"}


def _topic_words(*texts) -> set[str]:
    out = set()
    for t in texts:
        for w in re.findall(r"[a-z0-9]+", str(t or "").lower()):
            if len(w) > 2 and w not in _TOPIC_STOP:
                out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


UPLOAD_PICK_WINDOW = 4
UPLOAD_MAX_WAIT_DAYS = 10


def _next_upload_candidate():
    """Which ready long-form goes live next: of the UPLOAD_PICK_WINDOW oldest,
    the one least like the last two uploads (Jaccard over the PROJECT's topic
    words - name and description, not the packaging title, which is written
    to sound different on purpose), so the channel never runs near-identical
    topics back to back. Ties go to the oldest, and a video ready for
    UPLOAD_MAX_WAIT_DAYS goes first."""
    with state.connect() as con:
        ready = list(con.execute("SELECT * FROM videos WHERE status = 'uploading' AND youtube_video_id IS NULL "
                                 "ORDER BY created_at LIMIT ?", (UPLOAD_PICK_WINDOW,)))
        recent = list(con.execute("SELECT project_slug, project_title, project_description FROM videos "
                                  "WHERE uploaded_at IS NOT NULL ORDER BY uploaded_at DESC LIMIT 2"))
    if len(ready) < 2 or not recent:
        return ready[0] if ready else None
    try:
        ready_since = datetime.fromisoformat(ready[0]["updated_at"])
        if ready_since.tzinfo is None:
            ready_since = ready_since.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - ready_since > timedelta(days=UPLOAD_MAX_WAIT_DAYS):
            return ready[0]
    except (TypeError, ValueError):
        pass
    seen = [_topic_words(r["project_slug"], r["project_title"], r["project_description"]) for r in recent]

    def likeness(v) -> float:
        w = _topic_words(v["project_slug"], v["project_title"], v["project_description"])
        return max((len(w & s) / len(w | s)) if (w | s) else 0.0 for s in seen)

    return min(ready, key=likeness)          # min() keeps the oldest on ties


def _last_public_upload():
    """Most recent public upload of any kind (long-form, main Short, extra Short)."""
    ts = [t for t in (state.last_uploaded_at(), state.last_short_uploaded_at()) if t]
    return max(ts) if ts else None
_HELD_LOGGED: dict[tuple[int, str], str] = {}


def _log_held(v, stage: str, reason: str) -> None:
    """Log a pacing hold once per (video, stage, reason) instead of every tick."""
    key = (v["id"], stage)
    if _HELD_LOGGED.get(key) != reason:
        _HELD_LOGGED[key] = reason
        state.log("INFO", "orchestrator", f"video #{v['id']} {stage} held - {reason}")


def _can_upload_short_now() -> tuple[bool, str]:
    """Shorts have their own shelf: no weekly cap, but in the upload window
    and at least SHORT_MIN_GAP_HOURS after the previous Short of any kind."""
    if not _is_upload_window():
        return False, "outside upload window"
    last = state.last_short_uploaded_at()
    if last:
        gap_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if gap_h < SHORT_MIN_GAP_HOURS:
            return False, f"Shorts gap {gap_h:.1f}h/{SHORT_MIN_GAP_HOURS}h"
    return True, ""


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
    anyu = _last_public_upload()
    if anyu and (datetime.now(timezone.utc) - anyu).total_seconds() / 3600 < ANY_UPLOAD_GAP_HOURS:
        return False, f"within {ANY_UPLOAD_GAP_HOURS}h of another upload"
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
    if not HF_DEPLOY_ENABLED:
        state.update_video(v["id"], status="scripting")
        return
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


def _is_motion(v) -> bool:
    try:
        return json.loads(v["script"] or "{}").get("engine") == "motion"
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False


def _step_scripting(v) -> None:
    # Primary: motion-graphics storyboard on the most capable model. Any
    # failure other than a rate limit falls back to the classic screenplay
    # so a video is never blocked by the new engine.
    if MOTION_ENGINE:
        try:
            ws = _get_sibling_workspace(v["source_orchestrator"], v["project_slug"])
            script = motion_pipeline.script_long(v, ws)
            pk = script["packaging"]
            state.update_video(
                v["id"], status="recording", script=json.dumps(script), title=pk["title"],
                description=pk.get("description_intro", ""), tags=json.dumps(pk.get("tags", [])))
            return
        except claude_cli.RateLimited:
            raise
        except Exception as e:
            state.log("WARN", "orchestrator",
                      f"motion storyboard failed for #{v['id']}, using classic screenplay: {e}")
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
    if _is_motion(v):
        state.update_video(v["id"], status="rendering", video_path=motion_pipeline.record_long(v))
        return
    sp = json.loads(v["script"])
    out_dir = RENDER_DIR / f"video-{v['id']}"
    webm = recorder.record(sp, out_dir)
    state.update_video(v["id"], status="rendering", video_path=str(webm))


def _step_rendering(v) -> None:
    if _is_motion(v):
        fields = motion_pipeline.render_long(v, json.loads(v["script"]))
        state.update_video(v["id"], status="uploading", **fields)
        return
    sp = json.loads(v["script"])
    out_dir = RENDER_DIR / f"video-{v['id']}"
    audio_path = out_dir / "narration.mp3"
    srt_path = out_dir / "captions.srt"
    tts.synthesize_screenplay(sp, audio_path, srt_path)
    state.update_video(v["id"], tts_engine=tts.LAST_ENGINE)
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

    duration = _probe_duration_s(final_path)
    state.update_video(
        v["id"], status="uploading",
        audio_path=str(audio_path),
        final_path=str(final_path),
        srt_path=str(srt_path) if srt_path.exists() else None,
        thumbnail_path=str(thumb_path) if thumb_path else None,
        **({"duration_s": duration} if duration is not None else {}),
    )


def _step_uploading(v) -> None:
    # Record-first: the moment YouTube accepts the video its id is persisted.
    # A crash anywhere after that resumes here with youtube_video_id set and
    # skips straight to thumbnail/captions - it can never re-upload.
    if v["youtube_video_id"]:
        video_id, video_url = v["youtube_video_id"], v["youtube_url"]
    else:
        tags = json.loads(v["tags"]) if v["tags"] else []
        result = uploader.upload(
            video_path=Path(v["final_path"]),
            title=v["title"], description=v["description"], tags=tags,
            localizations=motion_pipeline.localizations(v),
        )
        video_id, video_url = result["video_id"], result["url"]
        state.update_video(v["id"], youtube_video_id=video_id, youtube_url=video_url,
                           uploaded_at=datetime.now(timezone.utc).isoformat())
    # Custom thumbnail. YouTube rate-limits thumbnail changes per channel; a
    # refused one is marked pending and retried before any back-catalogue work.
    if v["thumbnail_path"]:
        ok = False
        try:
            ok = uploader.upload_thumbnail(video_id=video_id, thumbnail_path=Path(v["thumbnail_path"]))
        except Exception as e:
            state.log("WARN", "orchestrator", f"thumbnail upload error: {e}")
        state.update_video(v["id"], thumb_pending=0 if ok else 1)
    # Caption track in English (skip if SRT missing).
    if v["srt_path"]:
        try:
            uploader.upload_caption(
                video_id=video_id, srt_path=Path(v["srt_path"]),
            )
        except Exception as e:
            state.log("WARN", "orchestrator", f"caption upload skipped: {e}")
        # German caption track: translated at RENDER time (no LLM call may
        # sit between "public on YouTube" and "recorded in the DB").
        srt_en = Path(v["srt_path"])
        de = srt_en.with_name(srt_en.stem + ".de.srt")
        # Only a German track produced from THIS render (never a stale file).
        if _is_motion(v) and de.exists() and srt_en.exists() and de.stat().st_mtime >= srt_en.stat().st_mtime:
            try:
                uploader.upload_caption(video_id=video_id, srt_path=de,
                                        language="de", name="Deutsch")
            except Exception as e:
                state.log("WARN", "orchestrator", f"German captions skipped: {e}")
    state.update_video(v["id"], status="shorts_scripting")


def _step_shorts_scripting(v) -> None:
    """Bespoke Shorts pipeline. Motion videos get a vertical motion-graphics
    Short; classic videos keep the classic Shorts screenplay."""
    if _is_motion(v):
        out_dir = Path(v["final_path"]).parent
        if motion_pipeline.is_motion_short(out_dir) and (out_dir / "shorts_metadata.json").exists():
            state.update_video(v["id"], status="shorts_recording")   # creative work already done
            return
        try:
            ws = _get_sibling_workspace(v["source_orchestrator"], v["project_slug"])
            motion_pipeline.script_short(v, ws, out_dir)
            state.update_video(v["id"], status="shorts_recording")
            return
        except claude_cli.RateLimited:
            raise
        except Exception as e:
            state.log("WARN", "orchestrator",
                      f"motion short failed for #{v['id']}, using classic Shorts script: {e}")
    sp = shorts_screenplay.write_shorts_screenplay(
        title=v["project_title"],
        description=v["project_description"],
        repo_url=v["project_repo_url"],
        pages_url=v["project_pages_url"],
    )
    md = shorts_metadata.write_shorts_metadata(
        title=v["project_title"],
        description=v["project_description"],
        repo_url=v["project_repo_url"],
        long_form_url=v["youtube_url"] or "",
        script="\n".join(s.get("voice_over", "") for s in sp["scenes"]),
    )
    # Reuse the script and metadata columns by stashing into the existing
    # ones — the long-form values are already on disk in renders/.
    out_dir = Path(v["final_path"]).parent
    (out_dir / "shorts_script.json").write_text(json.dumps(sp), encoding="utf-8")
    (out_dir / "shorts_metadata.json").write_text(json.dumps(md), encoding="utf-8")
    state.update_video(v["id"], status="shorts_recording")


def _step_shorts_recording(v) -> None:
    """Vertical 1080x1920 recording with a Shorts-specific scene list."""
    out_dir = Path(v["final_path"]).parent
    if motion_pipeline.is_motion_short(out_dir):
        state.update_video(v["id"], status="shorts_rendering")   # nothing to record
        return
    sp = json.loads((out_dir / "shorts_script.json").read_text(encoding="utf-8"))
    rec_dir = out_dir / "shorts_record"
    webm = recorder.record(sp, rec_dir, viewport=SHORTS_RESOLUTION)
    state.update_video(v["id"], status="shorts_rendering",
                       short_path=str(webm))  # repurpose short_path to track raw


def _step_shorts_rendering(v) -> None:
    """TTS + caption + ffmpeg compose into final vertical mp4."""
    out_dir = Path(v["final_path"]).parent
    if motion_pipeline.is_motion_short(out_dir):
        final_short, srt_path = motion_pipeline.render_short(v, out_dir)
        try:
            rep = shorts_quality.assess(video_path=final_short, srt_path=srt_path)
            state.log("INFO" if rep.passed else "ERROR", "shorts_quality",
                      f"video #{v['id']} {rep.summary()}\n" + shorts_quality.format_report(rep))
        except Exception as e:
            state.log("WARN", "shorts_quality", f"video #{v['id']} quality probe failed (non-fatal): {e}")
        state.update_video(v["id"], status="shorts_uploading", short_path=str(final_short))
        return
    sp = json.loads((out_dir / "shorts_script.json").read_text(encoding="utf-8"))
    audio_path = out_dir / "shorts_narration.mp3"
    srt_path = out_dir / "shorts_captions.srt"
    # shorts_mode=True -> 3-word cues (Netflix/TikTok pacing) instead of
    # the 7-word long-form cues. Same TTS audio, different SRT grouping.
    tts.synthesize_screenplay(sp, audio_path, srt_path, shorts_mode=True)

    raw_short = out_dir / "shorts_raw.mp4"
    compositor.compose(
        video_in=Path(v["short_path"]),
        audio_in=audio_path,
        output_path=raw_short,
    )
    final_short = out_dir / "shorts_final.mp4"
    try:
        editor.polish_short(
            main_video=raw_short,
            srt=srt_path if srt_path.exists() else None,
            output_path=final_short,
            hook_title=v["project_title"] or v["title"] or "",
        )
    except Exception as e:
        state.log("WARN", "orchestrator", f"shorts polish failed, shipping raw: {e}")
        import shutil
        shutil.copyfile(raw_short, final_short)
    short_duration = _probe_duration_s(final_short)
    if short_duration is not None:
        # Long-form duration_s is set during long-form rendering; we don't
        # overwrite it. Just log Shorts duration via WARN line so it shows
        # up in the meta report — Shorts duration is currently not stored
        # in a dedicated column; adding one would mean a migration.
        state.log("INFO", "orchestrator",
                  f"shorts duration #{v['id']}: {short_duration:.1f}s")

    # Quality gate — runs inline, free (ffmpeg probes only). Captures
    # per-render metrics for the meta-improver and surfaces failures
    # loudly via state.log. Shorts are bonus content (see fallback above),
    # so we log but do not block upload on quality failures — the captured
    # metrics let the Opus weekly reviewer drive upstream fixes.
    try:
        rep = shorts_quality.assess(
            video_path=final_short,
            srt_path=srt_path if srt_path.exists() else None,
        )
        level = "INFO" if rep.passed else "ERROR"
        state.log(level, "shorts_quality",
                  f"video #{v['id']} {rep.summary()}\n"
                  + shorts_quality.format_report(rep))
    except Exception as e:
        state.log("WARN", "shorts_quality",
                  f"video #{v['id']} quality probe failed (non-fatal): {e}")

    state.update_video(v["id"], status="shorts_uploading",
                       short_path=str(final_short))


def _step_embedding(v) -> None:
    """
    Cross-system integration: after both videos are live, update the
    source project's README to include the YouTube walkthrough links,
    commit, and push. Idempotent — re-runs are no-ops if links already
    present.

    Status flow: uploaded -> embedded (terminal success state).

    For 'manual_test' source: skip the embed (no real source repo to
    update). For autodev/agent-radar sources: locate the workspace,
    edit README.md, commit directly to main with a conventional commit
    message.
    """
    src = v["source_orchestrator"]
    if src not in ("autodev", "agent-radar"):
        state.update_video(v["id"], status="embedded")
        return

    workspace = _get_sibling_workspace(src, v["project_slug"])
    if workspace is None or not workspace.exists():
        state.log("WARN", "embedding",
                  f"workspace missing for {v['project_slug']}, skipping")
        state.update_video(v["id"], status="embedded")
        return

    readme = workspace / "README.md"
    if not readme.exists():
        state.log("WARN", "embedding",
                  f"no README.md in workspace for {v['project_slug']}")
        state.update_video(v["id"], status="embedded")
        return

    text = readme.read_text(encoding="utf-8")
    long_url = v["youtube_url"] or ""
    short_url = v["short_url"] or ""

    # Idempotency: if the long-form URL is already in the README, skip.
    if long_url and long_url in text:
        state.log("INFO", "embedding",
                  f"README already linked for {v['project_slug']}")
        state.update_video(v["id"], status="embedded")
        return

    # Build the link block — placed right after the H1 title, set off as
    # a blockquote so it visually stands out without looking marketing-y.
    link_lines: list[str] = []
    if long_url:
        link_lines.append(f"> **Video walkthrough:** {long_url}")
    if short_url:
        link_lines.append(f"> **60-second overview:** {short_url}")
    if not link_lines:
        state.update_video(v["id"], status="embedded")
        return
    link_block = "\n".join(link_lines)

    # Insert position: after first H1 line, skipping any immediately-
    # following blank lines so we land in a clean spot.
    lines = text.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_at = i + 1
            break
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1

    new_lines = lines[:insert_at] + ["", link_block, ""] + lines[insert_at:]
    new_text = "\n".join(new_lines).rstrip() + "\n"
    readme.write_text(new_text, encoding="utf-8")

    # Commit + push directly to main. This is a docs-only update to a
    # completed project; bypassing the branch+PR flow is fine.
    import subprocess

    def _git(args, check=False):
        return subprocess.run(
            ["git", *args], cwd=str(workspace),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=check,
        )

    try:
        _git(["checkout", "main"])
        _git(["pull", "--ff-only", "origin", "main"])
        _git(["add", "README.md"])
        cp = _git(["commit", "-m", "docs: add youtube walkthrough link"])
        if cp.returncode == 0:
            push = _git(["push", "origin", "main"])
            if push.returncode == 0:
                state.log("INFO", "embedding",
                          f"linked YouTube to {v['project_slug']} README")
            else:
                state.log("WARN", "embedding",
                          f"push failed for {v['project_slug']}: {push.stderr[-200:]}")
    except Exception as e:
        state.log("ERROR", "embedding", f"git push failed: {e}")

    state.update_video(v["id"], status="embedded")


def _step_shorts_uploading(v) -> None:
    """Upload the polished Short as a separate video. Failures here mark
    the main video 'uploaded' regardless — Shorts are a bonus."""
    out_dir = Path(v["final_path"]).parent
    md = json.loads((out_dir / "shorts_metadata.json").read_text(encoding="utf-8"))

    title = md.get("title") or (v["title"] or "")[:50]
    # Vertical + short is what makes a Short; "#Shorts" goes in the
    # description, not the title, so the title keeps its full length.
    description = md.get("description") or (
        f"Built autonomously. Full walkthrough: {v['youtube_url'] or ''}\n\n"
        f"AI disclosure: voice is AI-generated; code is real.\n\n#Shorts #AI #LLM"
    )
    tags = md.get("tags") or ["shorts", "ai", "llm", "agents"]
    if v["youtube_url"] and v["youtube_url"] not in description:
        description = f"Full video: {v['youtube_url']}\n\n{description}"   # cross-promote the long-form
    # Honesty guard: metadata written by older code (or by hand) must never
    # publish without the AI disclosure, in either language.
    ai = motion_pipeline.packaging.AI_DISCLOSURE
    if "ai disclosure" not in description.lower():
        head, sep, tail = description.rpartition("#Shorts")
        description = (f"{head.rstrip()}\n\n{ai['en']}\n\n{sep}{tail}" if sep
                       else f"{description.rstrip()}\n\n{ai['en']}")
    de_md = dict(md.get("de") or {})
    if de_md.get("description") and "ki-hinweis" not in de_md["description"].lower():
        de_md["description"] = de_md["description"].rstrip() + "\n\n" + ai["de"]
    md["de"] = de_md

    # Shorts are the reach engine, so a failed upload is RETRIED (the tick's
    # failure handling + revive) instead of being dropped. Record-first: the
    # id is persisted the moment YouTube accepts the Short, so a crash or a
    # caption error afterwards can never upload it twice.
    short_id = v["short_video_id"]
    if not short_id:
        de = md.get("de") or {}
        result = uploader.upload(
            video_path=Path(v["short_path"]),
            title=title, description=description, tags=tags[:15],
            localizations=({"de": {"title": de["title"][:100],
                                   "description": de.get("description", "")[:4900]}}
                           if de.get("title") else None),
        )
        short_id = result["video_id"]
        state.update_video(v["id"], short_video_id=short_id, short_url=result["url"],
                           short_uploaded_at=datetime.now(timezone.utc).isoformat())
    if motion_pipeline.is_motion_short(out_dir):
        srt = out_dir / "shorts_captions.srt"
        if srt.exists():
            try:
                uploader.upload_caption(video_id=short_id, srt_path=srt)
            except Exception as e:
                state.log("WARN", "orchestrator", f"Short captions skipped: {e}")
    # Next: embed the YouTube link into the source repo's README.
    state.update_video(v["id"], status="embedding")


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

    # Phase 1b — self-heal failed videos and alert a human if stuck.
    try:
        _revive_failed()
        if _TICK_COUNT % 4 == 1:   # every ~2h
            _watchdog()
    except Exception as e:
        state.log("WARN", "orchestrator", f"self-heal/watchdog error: {e}")

    # Phase 2 — drive the oldest in-flight video forward.
    PIPELINE = (
        "queued", "deploying", "scripting", "recording", "rendering",
        "uploading",
        "shorts_scripting", "shorts_recording", "shorts_rendering",
        "shorts_uploading",
        "embedding",
    )
    for status_to_drive in PIPELINE:
        v = (_resumable_upload(status_to_drive)
             or (_next_upload_candidate() if status_to_drive == "uploading" else None)
             or state.next_video_in_status(status_to_drive))
        if not v:
            continue

        # Mark the kickoff transition for "queued" so we don't pick it
        # twice across overlapping ticks. Goes to deploying first.
        if status_to_drive == "queued":
            state.update_video(v["id"], status="deploying")
            v = state.get_video(v["id"])
            status_to_drive = "deploying"

        # Upload steps are paced. A row whose upload already went through
        # (id recorded, crash before the step finished) is never re-gated.
        # When an upload is held, fall through to the later stages instead of
        # ending the tick, so other videos' Shorts/embedding keep moving.
        if status_to_drive == "uploading" and not v["youtube_video_id"]:
            ok, reason = _can_upload_now()
        elif status_to_drive == "shorts_uploading" and not v["short_video_id"]:
            ok, reason = _can_upload_short_now()
        else:
            ok, reason = True, ""
        if not ok:
            _log_held(v, status_to_drive, reason)
            continue

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
            elif status_to_drive == "shorts_scripting":
                _step_shorts_scripting(v)
            elif status_to_drive == "shorts_recording":
                _step_shorts_recording(v)
            elif status_to_drive == "shorts_rendering":
                _step_shorts_rendering(v)
            elif status_to_drive == "shorts_uploading":
                _step_shorts_uploading(v)
            elif status_to_drive == "embedding":
                _step_embedding(v)
        except claude_cli.RateLimited as e:
            # Claude is rate-limited: stages that need no Claude call
            # (uploads, embedding) must keep moving, so try the next stage.
            _safe_log("WARN", f"video #{v['id']} {status_to_drive}: {str(e)[:300]}")
            continue
        except Exception as e:
            _handle_step_failure(v, status_to_drive, e)
            return
        # A stage succeeded: reset the per-stage attempt budget, so an early
        # hiccup never eats the retries of a later stage.
        if v["attempt_count"]:
            state.update_video(v["id"], attempt_count=0)
        return  # one step per tick


POST_LIVE_STAGES = ("shorts_scripting", "shorts_recording", "shorts_rendering",
                    "shorts_uploading", "embedding")


def _safe_log(level: str, msg: str) -> None:
    try:
        state.log(level, "orchestrator", msg)
    except Exception:
        try:
            print(f"[{level}] orchestrator: {msg}".encode("ascii", "replace").decode(), flush=True)
        except Exception:
            pass


def _handle_step_failure(v, stage: str, e: Exception) -> None:
    attempts = (v["attempt_count"] or 0) + 1
    terminal = attempts >= MAX_VIDEO_RETRIES
    revives = v["revive_count"] or 0
    _safe_log("ERROR", f"video #{v['id']} {stage} failed (attempt {attempts}): {e}")
    # The long-form is already public: once retries AND revives are spent,
    # skip the bonus step (the Short / README backlink) and finish the video
    # instead of stranding a live video in failed_terminal forever.
    if terminal and stage in POST_LIVE_STAGES and v["youtube_video_id"] and revives >= MAX_REVIVES:
        nxt = "embedded" if stage == "embedding" else "embedding"
        state.update_video(v["id"], status=nxt, attempt_count=0, last_error=str(e)[:500])
        alerts.notify(f"skipped-{v['id']}-{stage}", f"Video #{v['id']}: gave up on {stage}",
                      f"{v['project_slug']}: long-form is live; skipped {stage} after all retries: {str(e)[:160]}",
                      level="ERROR")
        return
    fields = {"status": "failed_terminal" if terminal else stage, "attempt_count": attempts,
              "last_error": str(e)[:500]}
    if terminal:
        fields["failed_stage"] = stage
    state.update_video(v["id"], **fields)
    if terminal:
        plan = (f"auto-retry in {REVIVE_AFTER_HOURS}h (revive {revives + 1}/{MAX_REVIVES})"
                if revives < MAX_REVIVES else "NO more auto-retries - needs a human")
        alerts.notify(f"failed-{v['id']}-{revives}", f"Video #{v['id']} failed at {stage}",
                      f"{v['project_slug']}: {str(e)[:200]} -> {plan}", level="ERROR")


# --- Self-healing + watchdog -------------------------------------------- #

def _infer_failed_stage(r) -> str:
    """For rows that died before failed_stage existed."""
    if r["final_path"]:
        return "uploading"
    if r["video_path"]:
        return "rendering"
    if r["script"]:
        return "recording"
    return "scripting"


def _revive_failed() -> None:
    """Give failed_terminal videos another chance at the stage they died,
    after a cooldown. Outages (TTS, network, quota) are usually temporary;
    a video should never be lost to one."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=REVIVE_AFTER_HOURS)).isoformat()
    with state.connect() as con:
        rows = list(con.execute(
            "SELECT * FROM videos WHERE status = 'failed_terminal' "
            "AND COALESCE(revive_count, 0) < ? AND updated_at < ? ORDER BY id",
            (MAX_REVIVES, cutoff),
        ))
    for r in rows:
        stage = r["failed_stage"] or _infer_failed_stage(r)
        n = (r["revive_count"] or 0) + 1
        state.update_video(r["id"], status=stage, attempt_count=0, revive_count=n)
        state.log("INFO", "orchestrator",
                  f"revived video #{r['id']} ({r['project_slug']}) at {stage} "
                  f"(revive {n}/{MAX_REVIVES})")


def _watchdog() -> None:
    """Raise a human alert when the pipeline is silently stuck. This is the
    check that would have caught the Oct-Nov 2026 outage on day one."""
    with state.connect() as con:
        pending = con.execute(
            "SELECT COUNT(*) FROM videos WHERE status NOT IN ('embedded', 'uploaded', 'superseded')"
        ).fetchone()[0]
        dead = list(con.execute(
            "SELECT id, project_slug FROM videos WHERE status = 'failed_terminal' "
            "AND COALESCE(revive_count, 0) >= ?", (MAX_REVIVES,)))
        stuck = list(con.execute(
            # Only stages that are driven every tick. Upload and Shorts stages
            # legitimately wait behind the weekly pacing cap.
            "SELECT id, project_slug, status, updated_at FROM videos "
            "WHERE status IN ('queued', 'deploying', 'scripting', 'recording', "
            "'rendering', 'embedding') AND updated_at < ?",
            ((datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),)))
    last = state.last_uploaded_at()
    if pending and last:
        days = (datetime.now(timezone.utc) - last).days
        if days >= STALE_UPLOAD_ALERT_DAYS:
            alerts.notify("stale-uploads", f"No YouTube upload for {days} days",
                          f"{pending} video(s) pending. Run: python orchestrator.py health",
                          level="ERROR", cooldown_hours=24)
    if dead:
        alerts.notify("dead-videos", f"{len(dead)} video(s) gave up after {MAX_REVIVES} revives",
                      ", ".join(f"#{d['id']} {d['project_slug']}" for d in dead),
                      level="ERROR", cooldown_hours=24)
    for s in stuck:
        alerts.notify(f"stuck-{s['id']}-{s['status']}",
                      f"Video #{s['id']} stuck in {s['status']} for 48h+",
                      f"{s['project_slug']} (last update {s['updated_at'][:16]})",
                      cooldown_hours=24)


# --- Main loop ----------------------------------------------------------- #

def _self_heal_on_startup() -> None:
    # Bump rows orphaned in 'uploaded' (long-form succeeded but pipeline
    # advance was missed — e.g. uploaded by an older code path that ended
    # at 'uploaded' before the Shorts/embedding stages were chained on).
    with state.connect() as con:
        orphans = list(con.execute(
            "SELECT id, project_slug, short_video_id FROM videos "
            "WHERE status = 'uploaded'"
        ))
    for r in orphans:
        next_status = "embedding" if r["short_video_id"] else "shorts_scripting"
        state.update_video(r["id"], status=next_status)
        state.log("INFO", "orchestrator",
                  f"healed orphan #{r['id']} ({r['project_slug']}): uploaded -> {next_status}")

    with state.connect() as con:
        rows = list(con.execute(
            "SELECT id, status, project_slug FROM videos WHERE status NOT IN "
            "('uploaded', 'embedded', 'failed_terminal', 'superseded', 'held_for_upgrade')"
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


def _maybe_backfill_thumbnails() -> None:
    """Custom thumbnails under YouTube's per-channel thumbnail rate limit.
    1) NEW videos whose thumbnail was refused (thumb_pending): retried every
       hour (a new video's first day decides its reach), always first.
    2) Back catalogue: at most BACKFILL_PER_DAY per 24 h, only OUTSIDE the
       upload window (so a new upload always finds allowance left), stopping
       at the first refusal."""
    from config import LOCAL_HOME
    base = LOCAL_HOME / "backfill_thumbs"
    base.mkdir(parents=True, exist_ok=True)

    def due(marker: Path, hours: float) -> bool:
        return not marker.exists() or time.time() - marker.stat().st_mtime >= hours * 3600

    pend_marker, back_marker = base / ".last_pending_try", base / ".last_backfill"
    if due(pend_marker, 1):
        with state.connect() as con:
            pending = list(con.execute(
                "SELECT id, youtube_video_id, thumbnail_path FROM videos WHERE thumb_pending = 1 "
                "AND youtube_video_id IS NOT NULL AND thumbnail_path IS NOT NULL ORDER BY id"))
        if pending:
            pend_marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        for r in pending:
            if not Path(r["thumbnail_path"]).exists():
                state.update_video(r["id"], thumb_pending=0)
                continue
            if uploader.upload_thumbnail(video_id=r["youtube_video_id"], thumbnail_path=Path(r["thumbnail_path"])):
                state.update_video(r["id"], thumb_pending=0)
                state.log("INFO", "orchestrator", f"pending thumbnail applied for #{r['id']}")
            else:
                return                      # still rate-limited; the back catalogue waits too
    with state.connect() as con:
        still_pending = con.execute("SELECT COUNT(*) FROM videos WHERE thumb_pending = 1").fetchone()[0]
    if (still_pending or _is_upload_window() or not due(back_marker, 24)
            or not (base / "RENDERED").exists() or (base / "APPLIED_ALL").exists()):
        return
    back_marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backfill_thumbnails", BASE_DIR / "tools" / "backfill_thumbnails.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        n = mod.apply(max_n=BACKFILL_PER_DAY)
        state.log("INFO", "orchestrator", f"thumbnail backfill: applied {n} today")
    except Exception as e:
        state.log("WARN", "orchestrator", f"thumbnail backfill error: {e}")


BACKFILL_PER_DAY = 4


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


def _maybe_run_monitor() -> None:
    """Daily YouTube stats poll — feeds evidence to the meta-improver."""
    if not monitor.should_run_monitor():
        return
    try:
        monitor.run_monitor()
    except Exception as e:
        state.log("ERROR", "orchestrator", f"monitor failed: {e}")


def run_forever() -> None:
    state.init_db()
    state.log("INFO", "orchestrator", f"launchpad starting (pid={os.getpid()})")
    if not _preflight_ok():
        sys.exit(1)
    _self_heal_on_startup()
    while True:
        try:
            tick()
            try:
                extra_shorts.step()       # independent reach-multiplier queue
            except Exception as e:
                state.log("WARN", "orchestrator", f"extra Shorts step error: {e}")
            _maybe_run_monitor()
            _maybe_backfill_thumbnails()
            _maybe_run_meta()
            claude_cli.maybe_weekly_cli_update()
        except Exception:
            err = traceback.format_exc()
            _safe_log("ERROR", f"unhandled tick error:\n{err}")
            try:   # if even the DB is broken (disk full, locked), still tell a human
                alerts.notify("unhandled-tick-error", "launchpad loop error", err[-220:], level="ERROR",
                              cooldown_hours=6)
            except Exception:
                pass
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
              "shorts_scripting", "shorts_recording", "shorts_rendering",
              "shorts_uploading", "embedding", "uploaded", "embedded",
              "held_for_upgrade", "failed_terminal", "superseded"):
        print(f"  {s:18s} {counts.get(s, 0)}")
    print(f"\nuploads in last 7 days: {state.uploads_in_window(168)}/{MAX_UPLOADS_PER_WEEK}")
    print(f"claude calls in last 5h: {state.claude_calls_in_window(5)}")


def _print_health() -> None:
    print("\n=== launchpad health ===\n")
    print(f"YouTube auth:     {'YES' if YT_TOKEN_PATH.exists() else 'NO — run `python orchestrator.py auth`'}")
    _print_status()
    last = state.last_uploaded_at()
    if last:
        days = (datetime.now(timezone.utc) - last).days
        flag = "  <-- STALE" if days >= STALE_UPLOAD_ALERT_DAYS else ""
        print(f"\nlast upload:      {last:%Y-%m-%d %H:%M} UTC ({days} days ago){flag}")
    with state.connect() as con:
        for r in con.execute(
            "SELECT id, project_slug, failed_stage, COALESCE(revive_count,0) AS rc, "
            "substr(last_error, 1, 90) AS err FROM videos WHERE status = 'failed_terminal'"
        ):
            print(f"FAILED #{r['id']} {r['project_slug']} at {r['failed_stage']} "
                  f"(revives {r['rc']}/{MAX_REVIVES}): {r['err']}")
    print(f"voice engines:    edge -> kokoro (local) -> windows sapi; "
          f"local venv {'OK' if tts.TTS_VENV_PY.exists() else 'MISSING'}")
    print(f"alerts file:      {alerts.ALERT_FILE}")


def main() -> None:
    parser = argparse.ArgumentParser(description="launchpad — autonomous YouTube agent")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "tick", "scan", "auth", "preflight",
                                 "status", "health", "meta", "monitor", "force_test"])
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
    elif args.command == "monitor":
        n = monitor.run_monitor()
        print(f"recorded stats for {n} video(s)")
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
