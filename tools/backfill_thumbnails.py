"""
One-off: design and (optionally) apply custom thumbnails for already-uploaded
long-form videos. Until the channel was phone-verified on 2026-11-20 every
thumbnails.set call was rejected, so the whole back catalogue shows YouTube's
auto-picked frames.

  python tools/backfill_thumbnails.py render   # concepts + render + contact sheet (no upload)
  python tools/backfill_thumbnails.py apply    # upload the rendered thumbnails

Concepts come from one call on the most capable model; the visual is a real
screenshot of the project's GitHub page. Quota: thumbnails.set = 50 units.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import nowindow  # noqa: E402,F401
import claude_cli  # noqa: E402
import state  # noqa: E402
import uploader  # noqa: E402
from agents.storyboard import PLAYBOOK  # noqa: E402
from config import EFFORT_CREATIVE, LOCAL_HOME, MODEL_CREATIVE  # noqa: E402
from motion import capture, thumb  # noqa: E402

OUT = LOCAL_HOME / "backfill_thumbs"

PROMPT = """You design YouTube thumbnail text for a developer channel (AI/ML open-source projects; audience developers and
AI/ML hiring managers). For each video below write:
- text: 2-4 punchy words for the thumbnail that complement (not repeat) the title and create curiosity or name a payoff
- emphasis: exactly one word from text to highlight
- pill: a 1-3 word topic tag in caps (e.g. "AI AGENTS", "LLM EVALS", "RAG")
Rules: no clickbait the video does not deliver, no emoji, plain words that read at phone size.

{playbook}

VIDEOS (id | title | project description):
{videos}

Return only a JSON array: [{{"id": 1, "text": "", "emphasis": "", "pill": ""}}, ...] with one entry per video."""


def _videos() -> list:
    with state.connect() as con:
        return list(con.execute(
            "SELECT id, title, project_title, project_description, project_repo_url, youtube_video_id "
            "FROM videos WHERE youtube_video_id IS NOT NULL ORDER BY id"))


def render() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    vids = _videos()
    concepts_path = OUT / "concepts.json"
    if concepts_path.exists():
        concepts = json.loads(concepts_path.read_text(encoding="utf-8"))
    else:
        listing = "\n".join(f"{v['id']} | {v['title']} | {(v['project_description'] or '')[:160]}" for v in vids)
        raw = claude_cli.call_claude(PROMPT.format(playbook=PLAYBOOK, videos=listing), component="thumb_backfill",
                                     model=MODEL_CREATIVE, effort=EFFORT_CREATIVE, allowed_tools=[], timeout_s=1200)
        concepts = claude_cli.extract_json(raw)
        concepts_path.write_text(json.dumps(concepts, indent=1), encoding="utf-8")
    by_id = {int(c["id"]): c for c in concepts if isinstance(c, dict) and "id" in c}
    made = []
    for v in vids:
        c = by_id.get(v["id"])
        if not c:
            continue
        d = OUT / f"video-{v['id']}"
        shot = d / "repo.png"
        if not shot.exists():
            capture.repo_screen(v["project_repo_url"], shot)
        vis = None
        if shot.exists():
            from PIL import Image
            im = Image.open(shot)
            im.crop((0, 0, im.width, min(im.height, int(im.width * 0.625)))).save(d / "visual.png")
            vis = d / "visual.png"
        out = thumb.render_thumbnail(text=c["text"], emphasis=c.get("emphasis", ""), visual_png=vis,
                                     out_jpg=d / "thumbnail.jpg", pill=(c.get("pill") or "OPEN SOURCE")[:22])
        made.append((v["id"], out))
    # contact sheet for human/AI review
    from PIL import Image
    w, h, cols = 480, 270, 4
    rows = (len(made) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * w, rows * h))
    for i, (_, p) in enumerate(made):
        sheet.paste(Image.open(p).convert("RGB").resize((w, h)), ((i % cols) * w, (i // cols) * h))
    sheet.save(OUT / "contact_sheet.jpg", quality=88)
    (OUT / "RENDERED").write_text(str(len(made)))


def apply(max_n: int | None = None) -> int:
    """Upload rendered thumbnails not yet applied. Stops at the first failure
    (YouTube rate-limits thumbnail changes per channel), so it is safe to
    call repeatedly; progress is kept in applied.json. Returns count applied."""
    done_path = OUT / "applied.json"
    done = set(json.loads(done_path.read_text())) if done_path.exists() else set()
    n = 0
    remaining = 0
    for v in _videos():
        p = OUT / f"video-{v['id']}" / "thumbnail.jpg"
        if v["id"] in done or not p.exists():
            continue
        remaining += 1
        if (max_n is not None and n >= max_n):
            continue
        if not uploader.upload_thumbnail(video_id=v["youtube_video_id"], thumbnail_path=p):
            break
        done.add(v["id"])
        n += 1
        remaining -= 1
        done_path.write_text(json.dumps(sorted(done)))
    (OUT / "APPLIED").write_text(str(len(done)))
    if remaining == 0:
        (OUT / "APPLIED_ALL").write_text(str(len(done)))
    return n


if __name__ == "__main__":
    {"render": render, "apply": apply}[sys.argv[1]]()
