"""
The motion-graphics pipeline, step by step. orchestrator.py calls these from
its state machine when a video's script carries {"engine": "motion"}; videos
scripted the classic way keep using the old recorder path.

  script_long   storyboard (writer + editor) -> validate -> packaging
  record_long   real screenshot of the GitHub page (headless)
  render_long   per-scene voice -> frame render -> captions -> chapters -> thumbnail
  script_short  vertical storyboard + packaging for the Short
  render_short  vertical render
  german_captions  translated caption track for upload
"""
from __future__ import annotations

import json
from pathlib import Path

import state
from agents import packaging, repo_context, storyboard
from config import RENDER_DIR
from motion import capture, render, thumb, timeline, validate

VISUAL_TYPES = ("diagram", "code", "terminal", "compare", "stat", "stats", "bullets")


def sibling_workspace(source_orchestrator: str, project_slug: str) -> Path | None:
    """The project's local clone path from autodev's / agent-radar's DB (read-only)."""
    import sqlite3
    from config import AGENT_RADAR_DB, AUTODEV_DB
    db = {"autodev": AUTODEV_DB, "agent-radar": AGENT_RADAR_DB}.get(source_orchestrator)
    if not db or not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT workspace_path FROM projects WHERE slug = ?", (project_slug,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    return Path(row["workspace_path"]) if row and row["workspace_path"] else None


def _ctx(v, workspace: Path | None) -> dict:
    return repo_context.gather(workspace, title=v["project_title"] or "", description=v["project_description"] or "",
                               repo_url=v["project_repo_url"] or "")


def _log(msg: str) -> None:
    state.log("INFO", "motion", msg)


def _validated(v, sb: dict, *, kind: str, ctx: dict) -> dict:
    """Integrity guard + at most one targeted rewrite for unsupported numbers."""
    sb, notes = validate.normalise(sb, kind=kind, ctx=ctx, repo_url=v["project_repo_url"])
    if sb.get("unsupported_claims"):
        try:
            fixed = storyboard.fix_claims(sb, sb["unsupported_claims"], ctx)
            sb, notes2 = validate.normalise(fixed, kind=kind, ctx=ctx, repo_url=v["project_repo_url"])
            notes += ["after fix pass: " + "; ".join(notes2)] if notes2 else ["fix pass cleared all claims"]
        except Exception as e:
            from claude_cli import RateLimited
            if isinstance(e, RateLimited):
                raise
            notes.append(f"fix pass failed: {str(e)[:150]}")
    # Fail closed: anything still unverified is cut mechanically.
    for _ in range(2):
        if not sb.get("unsupported_claims"):
            break
        notes.append(f"scrubbed {sb['unsupported_claims']}")
        sb, _n = validate.normalise(validate.scrub(sb, sb["unsupported_claims"]), kind=kind, ctx=ctx,
                                    repo_url=v["project_repo_url"])
    if sb.get("unsupported_claims"):
        raise ValueError(f"unverifiable claims survived scrubbing: {sb['unsupported_claims']}")
    if notes:
        state.log("INFO", "motion", f"video #{v['id']} {kind} storyboard checks: {'; '.join(notes)[:900]}")
    return sb


def _fallback_package(v) -> dict:
    """Deterministic packaging so a flaky packaging call never blocks a video."""
    title = (v["project_title"] or v["project_slug"] or "Open-source AI project")[:90]
    desc = (v["project_description"] or "").strip()
    return {"title": title, "description_intro": desc[:300], "description_body": "", "tags": [],
            "hashtags": ["AI", "LLM", "OpenSource"], "thumbnail": {"text": title.split(":")[0][:28]},
            "de": {}, "fallback": True}


def script_long(v, workspace: Path | None) -> dict:
    out_dir = RENDER_DIR / f"video-{v['id']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / "storyboard_long.json"   # never redo minutes of Fable work on a retry
    ctx = _ctx(v, workspace)
    if cache.exists():
        sb = json.loads(cache.read_text(encoding="utf-8"))
    else:
        sb = storyboard.write_storyboard(kind="long", title=v["project_title"], description=v["project_description"],
                                         repo_url=v["project_repo_url"], ctx=ctx)
        cache.write_text(json.dumps(sb, indent=1), encoding="utf-8")
    sb = _validated(v, sb, kind="long", ctx=ctx)
    pk = repackage_long(v, sb)
    cache.unlink(missing_ok=True)
    return {"engine": "motion", "storyboard": sb, "packaging": pk}


def other_titles(v, limit: int = 15) -> list[str]:
    """Titles subscribers saw recently or will see next (never this video's),
    so packaging can pick a clearly different angle."""
    from config import MAX_REVIVES
    with state.connect() as con:
        rows = con.execute(      # parked rows with revives left will still be published
            "SELECT title FROM videos WHERE id != ? AND COALESCE(title, '') != '' AND "
            "(uploaded_at IS NOT NULL OR status IN ('recording', 'rendering', 'uploading') "
            " OR (status = 'failed_terminal' AND COALESCE(revive_count, 0) < ?)) "
            "ORDER BY COALESCE(uploaded_at, '9999') DESC, id DESC LIMIT ?",
            (v["id"], MAX_REVIVES, limit * 2)).fetchall()
    return [r["title"] for r in rows if not packaging._looks_german(r["title"])][:limit]


def repackage_long(v, sb: dict, attempts: int = 3) -> dict:
    """Packaging with up to `attempts` tries (each retry is told why the last
    answer was rejected), then a deterministic fallback."""
    from claude_cli import RateLimited
    note = ""
    others = other_titles(v)
    for attempt in range(attempts):
        try:
            return packaging.package_long(title=v["project_title"], description=v["project_description"],
                                          repo_url=v["project_repo_url"], storyboard=sb,
                                          other_titles=others, note=note)
        except RateLimited:
            raise
        except Exception as e:
            state.log("WARN", "motion", f"video #{v['id']} packaging attempt {attempt + 1} failed: {str(e)[:200]}")
            note = packaging.retry_note(e)
    import alerts             # never silent: a generic package costs reach, a human should know
    alerts.notify(f"pkg-fallback-{v['id']}", f"Video #{v['id']}: generic fallback packaging",
                  f"{attempts} packaging answers were rejected (last: {note[:200]}). The video still ships "
                  "with its project name as title; re-run packaging if you want a better one.")
    return _fallback_package(v)


def record_long(v) -> str:
    out_dir = RENDER_DIR / f"video-{v['id']}"
    shot = capture.repo_screen(v["project_repo_url"], out_dir / "repo.png")
    return str(shot) if shot else ""


def _images(v) -> dict:
    p = Path(v["video_path"]) if v["video_path"] else None
    return {"repo": p.as_uri()} if p and p.exists() and p.suffix == ".png" else {}


def _drop_missing_browser(tl: dict, images: dict, repo_url: str) -> None:
    if images.get("repo"):
        return
    for s in tl["scenes"]:
        if s["type"] == "browser":
            s["type"] = "statement"
            s["visual"] = {"headline": "Open source on GitHub", "sub": repo_url.replace("https://", "")}


def render_long(v, script: dict) -> dict:
    """Returns fields for state.update_video."""
    out_dir = RENDER_DIR / f"video-{v['id']}"
    sb, pk = script["storyboard"], script["packaging"]
    tl = timeline.build(sb["scenes"], out_dir / "long")
    images = _images(v)
    _drop_missing_browser(tl, images, v["project_repo_url"] or "")
    final = render.render_video(tl, out_dir / "final.mp4", vertical=False, images=images, log=_log)
    srt = out_dir / "captions.srt"
    srt.with_name("captions.de.srt").unlink(missing_ok=True)   # never ship a stale translation
    srt.write_text(timeline.to_srt(tl["words"]), encoding="utf-8")
    try:
        german_captions(srt)            # captions.de.srt, ready before upload
    except Exception as e:
        state.log("WARN", "motion", f"German captions failed for #{v['id']} (non-fatal): {str(e)[:200]}")
    chapters = timeline.chapters(tl["scenes"], duration=tl["duration"])
    desc_en = packaging.build_description(pk, chapters=chapters, repo_url=v["project_repo_url"])
    desc_de = packaging.build_description(pk, chapters=chapters, repo_url=v["project_repo_url"], lang="de")

    thumb_path = None
    try:
        th = pk.get("thumbnail") or {}
        idx = th.get("visual_scene")
        scenes = tl["scenes"]
        pick = scenes[idx] if isinstance(idx, int) and 0 <= idx < len(scenes) and scenes[idx]["type"] in VISUAL_TYPES \
            else next((s for s in scenes if s["type"] in ("diagram", "code")), None) \
            or next((s for s in scenes if s["type"] in VISUAL_TYPES), None)
        vis = None
        if pick:
            t = round(pick["start"] + (pick["end"] - pick["start"]) * 0.85, 2)
            frame = render.snapshot_frames(tl, out_dir / "thumb", [t], vertical=False, images=images, clean=True)[0]
            vis = thumb.crop_center(frame, out_dir / "thumb" / "visual.png", 0.9, 0.86)
        thumb_path = thumb.render_thumbnail(text=th.get("text") or pk["title"], emphasis=th.get("emphasis") or "",
                                            visual_png=vis, out_jpg=out_dir / "thumbnail.jpg")
    except Exception as e:
        state.log("WARN", "motion", f"thumbnail render failed for #{v['id']}: {e}")

    script = dict(script)
    script["rendered"] = {"title_de": (pk.get("de") or {}).get("title", ""), "description_de": desc_de,
                          "chapters": chapters, "duration": tl["duration"], "engines": tl["engines"]}
    return {"final_path": str(final), "audio_path": str(tl["audio"]), "srt_path": str(srt),
            "thumbnail_path": str(thumb_path) if thumb_path else None, "description": desc_en,
            "duration_s": tl["duration"], "tts_engine": ",".join(sorted(set(tl["engines"]))) or None,
            "script": json.dumps(script)}


def localizations(v) -> dict | None:
    try:
        script = json.loads(v["script"] or "{}")
    except json.JSONDecodeError:
        return None
    r = script.get("rendered") or {}
    if script.get("engine") != "motion" or not r.get("title_de"):
        return None
    return {"de": {"title": r["title_de"][:100], "description": r.get("description_de", "")[:4900]}}


def german_captions(srt_en: Path) -> Path | None:
    """Translate an English SRT into German, keeping every timing."""
    blocks = [b for b in srt_en.read_text(encoding="utf-8").strip().split("\n\n") if b.strip()]
    heads, texts = [], []
    for b in blocks:
        lines = b.splitlines()
        if len(lines) < 3:
            continue
        heads.append(lines[:2])
        texts.append(" ".join(lines[2:]))
    if not texts:
        return None
    de = packaging.translate_lines(texts)
    out = srt_en.with_name(srt_en.stem + ".de.srt")
    out.write_text("\n\n".join("\n".join(h + [t]) for h, t in zip(heads, de)) + "\n", encoding="utf-8")
    return out


def script_short(v, workspace: Path | None, out_dir: Path, angle: str = "") -> None:
    ctx = _ctx(v, workspace)
    sb = storyboard.write_storyboard(kind="short", title=v["project_title"], description=v["project_description"],
                                     repo_url=v["project_repo_url"], ctx=ctx, angle=angle)
    sb = _validated(v, sb, kind="short", ctx=ctx)
    from claude_cli import RateLimited
    pk, note = None, ""
    for attempt in range(3):           # never throw away the storyboard over one bad packaging answer
        try:
            pk = packaging.package_short(title=v["project_title"], description=v["project_description"],
                                         storyboard=sb, note=note)
            break
        except RateLimited:
            raise
        except Exception as e:
            if attempt == 2:
                raise
            state.log("WARN", "motion", f"video #{v['id']} short packaging attempt {attempt + 1} failed: {str(e)[:200]}")
            note = packaging.retry_note(e)
    out_dir.mkdir(parents=True, exist_ok=True)
    desc = (pk.get("description") or "").strip()
    links = []
    if v["youtube_url"]:
        links.append(f"Full video: {v['youtube_url']}")
    if v["project_repo_url"]:
        links.append(f"Code: {v['project_repo_url']}")
    disclosure = packaging.AI_DISCLOSURE["en"]
    de = dict(pk.get("de") or {})
    if de.get("description"):
        de["description"] = de["description"].strip() + "\n\n" + packaging.AI_DISCLOSURE["de"]
    md = {"title": pk["title"], "tags": pk.get("tags") or [],
          "description": "\n\n".join([p for p in (desc, "\n".join(links), disclosure, "#Shorts #AI #LLM") if p]),
          "de": de}
    (out_dir / "shorts_metadata.json").write_text(json.dumps(md), encoding="utf-8")
    # Written LAST: its presence means all creative work for this Short is done.
    (out_dir / "shorts_script.json").write_text(json.dumps({"engine": "motion", "storyboard": sb, "packaging": pk}),
                                                encoding="utf-8")


def render_short(v, out_dir: Path) -> tuple[Path, Path]:
    sp = json.loads((out_dir / "shorts_script.json").read_text(encoding="utf-8"))
    tl = timeline.build(sp["storyboard"]["scenes"], out_dir / "short")
    repo_png = out_dir / "repo.png"
    parent_png = out_dir.parent / "repo.png"          # extra Shorts live in video-N/extra-K/
    if not repo_png.exists() and parent_png.exists():
        repo_png = parent_png
    images = {"repo": repo_png.as_uri()} if repo_png.exists() else {}
    if not images and any(s["type"] == "browser" for s in tl["scenes"]):
        shot = capture.repo_screen(v["project_repo_url"], repo_png)
        if shot:
            images = {"repo": shot.as_uri()}
    _drop_missing_browser(tl, images, v["project_repo_url"] or "")
    final = render.render_video(tl, out_dir / "shorts_final.mp4", vertical=True, images=images, log=_log)
    srt = out_dir / "shorts_captions.srt"
    srt.write_text(timeline.to_srt(tl["words"], words_per_cue=3), encoding="utf-8")
    return final, srt


def is_motion_short(out_dir: Path) -> bool:
    p = out_dir / "shorts_script.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("engine") == "motion"
    except Exception:
        return False
