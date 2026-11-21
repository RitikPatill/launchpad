"""
One-off / on-demand re-render for a Short whose recording already exists.

Use case: a Short shipped with audio distortion / oversized captions /
some other pipeline-side defect that doesn't require re-recording the
underlying screen capture. We re-synthesize the narration (with the new
caption-density mode), re-mux through the loudness-normalized compositor,
re-polish with the Netflix-style caption styling, run the new quality
gate, and (optionally) publish as a new YouTube Short while retiring
the old one to private.

Usage:
    python rerun_shorts.py <video_id>                   # render only, no upload
    python rerun_shorts.py <video_id> --upload          # render + upload as new
    python rerun_shorts.py <video_id> --upload --retire # also private the old short

Notes:
  - Reuses the existing screen recording in `<RENDER_DIR>/video-<id>/shorts_record/`.
    If multiple .webm files exist (rare), picks the most-recently-modified.
  - Overwrites `shorts_narration.mp3`, `shorts_captions.srt`,
    `shorts_raw.mp4`, and `shorts_final.mp4` for that video. The originals
    are not backed up — this is intentional, the canonical artifact is
    the YouTube upload.
  - Quality gate failure aborts the upload step but always leaves the
    rendered .mp4 on disk for inspection.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the launchpad/ module-level imports resolve when run as a script.
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import compositor  # noqa: E402
import state  # noqa: E402
import tts  # noqa: E402
import uploader  # noqa: E402
from agents import editor, shorts_quality  # noqa: E402
from config import RENDER_DIR  # noqa: E402


def _pick_webm(rec_dir: Path) -> Path:
    candidates = sorted(rec_dir.glob("*.webm"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no .webm in {rec_dir}")
    return candidates[0]


def rerun(video_id: int, *, do_upload: bool, do_retire: bool) -> None:
    v = state.get_video(video_id)
    if not v:
        raise SystemExit(f"no video row with id={video_id}")

    out_dir = RENDER_DIR / f"video-{video_id}"
    rec_dir = out_dir / "shorts_record"
    script_path = out_dir / "shorts_script.json"
    metadata_path = out_dir / "shorts_metadata.json"

    if not script_path.exists():
        raise SystemExit(f"shorts_script.json missing at {script_path}")
    if not rec_dir.exists():
        raise SystemExit(f"shorts_record dir missing at {rec_dir}")

    sp = json.loads(script_path.read_text(encoding="utf-8"))
    md = (
        json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata_path.exists() else {}
    )
    webm = _pick_webm(rec_dir)
    print(f"[1/5] using recording: {webm.name}")

    audio_path = out_dir / "shorts_narration.mp3"
    srt_path = out_dir / "shorts_captions.srt"
    print(f"[2/5] synthesizing TTS (3-word cues, Netflix-style pacing)")
    tts.synthesize_screenplay(sp, audio_path, srt_path, shorts_mode=True)

    raw_short = out_dir / "shorts_raw.mp4"
    print(f"[3/5] compose with loudnorm I=-16 TP=-1.5")
    compositor.compose(
        video_in=webm, audio_in=audio_path, output_path=raw_short,
    )

    final_short = out_dir / "shorts_final.mp4"
    title = (v["project_title"] or v["title"] or "").strip() or "Project"
    print(f"[4/5] polish + caption burn-in + hook")
    editor.polish_short(
        main_video=raw_short,
        srt=srt_path if srt_path.exists() else None,
        output_path=final_short,
        hook_title=title,
    )

    print(f"[5/5] quality gate")
    rep = shorts_quality.assess(
        video_path=final_short,
        srt_path=srt_path if srt_path.exists() else None,
    )
    print(shorts_quality.format_report(rep))

    if not rep.passed:
        print("\nQUALITY GATE FAILED — aborting upload. Output still at:")
        print(f"  {final_short}")
        sys.exit(2)

    if not do_upload:
        print(f"\nrendered (no upload): {final_short}")
        return

    # --- Upload as a new Short ---
    yt_title = (md.get("title") or title)[:100]
    if "#shorts" not in yt_title.lower():
        yt_title = (yt_title[:42] + " #Shorts")[:100]
    description = md.get("description") or (
        f"Built autonomously by my multi-agent system.\n\n"
        f"Full walkthrough: {v['youtube_url'] or ''}\n\n"
        f"AI disclosure: voice is AI-generated; code is real.\n\n#Shorts #AI #LLM"
    )
    tags = md.get("tags") or ["shorts", "ai", "llm", "agents"]

    print(f"\nuploading new Short to YouTube...")
    new = uploader.upload(
        video_path=final_short,
        title=yt_title,
        description=description,
        tags=tags[:15],
    )
    print(f"  -> {new['url']}")

    try:
        uploader.upload_caption(video_id=new["video_id"], srt_path=srt_path)
    except Exception as e:
        print(f"  (caption sidecar upload skipped: {e})")

    old_short_id = v["short_video_id"]
    if do_retire and old_short_id and old_short_id != new["video_id"]:
        print(f"setting old short {old_short_id} -> private")
        try:
            uploader.set_video_privacy(video_id=old_short_id, privacy="private")
        except Exception as e:
            print(f"  (privacy update failed: {e})")

    state.update_video(
        video_id,
        short_video_id=new["video_id"],
        short_url=new["url"],
        short_path=str(final_short),
    )
    print(f"\nDB row #{video_id} updated. Done.")
    print(f"  new short:  {new['url']}")
    if old_short_id:
        print(f"  old short:  https://youtu.be/{old_short_id}  (private)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id", type=int)
    ap.add_argument("--upload", action="store_true",
                    help="upload the rendered Short to YouTube as a new video")
    ap.add_argument("--retire", action="store_true",
                    help="when --upload set, also privacy=private the previous short")
    args = ap.parse_args()
    state.init_db()
    rerun(args.video_id, do_upload=args.upload, do_retire=args.retire)
