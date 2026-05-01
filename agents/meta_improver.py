"""
Meta-improver agent — the Opus self-improvement layer for launchpad.

Runs WEEKLY. Reads:
  - All videos shipped in the last 7 days (their scripts, metadata,
    thumbnails, captions, YouTube URLs)
  - The current agent prompts in agents/*.py
  - YouTube performance signals where available (views, like ratio,
    comments — pulled from the YouTube Data API on each video)
  - The compositor + recorder + thumbnail config knobs

Decides:
  - Screenplay: are voice-over scripts engaging? Too dry? Too long?
  - Metadata: are titles winning the A/B candidates? Are descriptions
    formatted well? Are tags reaching the right audience?
  - Thumbnail: are the PIL composites readable at thumbnail size?
  - Pacing: are we uploading at good times?

Outputs:
  - A weekly review markdown at meta_reports/<date>.md
  - Up to MAX_META_EDITS_PER_RUN small edits to prompt strings in
    agents/screenplay.py and agents/metadata.py
  - Records each edit in the prompt_versions audit table

Why Opus 4.7 instead of Sonnet (the rest of launchpad uses Sonnet only):
critique and creative judgment about what would make a video viral
benefits genuinely from the deeper model. One Opus call per week is
small — at the cost of one Sonnet conversation, you get back compounding
improvement to every video the system makes after.

Edits are scoped: ONLY prompt-string constants in agents/*.py. The agent
cannot touch orchestrator/state/config/publisher/recorder/etc., so even
a stuck or hallucinating meta-improver can't break the pipeline.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import claude_cli
import state
from config import BASE_DIR


META_MODEL = "claude-opus-4-7"
MAX_META_EDITS_PER_RUN = 2


PROMPT = """You are the META-IMPROVER for launchpad — a Sonnet-only
multi-agent system that ships YouTube walkthrough videos for AI agent
projects on Ritik's GitHub. Your job: make the worker agents' output
genuinely better at attracting views, retention, and comments.

YOU ARE RUNNING ON OPUS 4.7. Use the deeper analysis budget. Sonnet
runs every other agent in this system; you are the only Opus call.

CONTEXT:
- launchpad pipeline per video: screenplay (Sonnet) -> recorder
  (Playwright) -> tts (Edge TTS) -> compositor (ffmpeg) -> thumbnail
  (PIL composite) -> metadata (Sonnet x2 for A/B title) -> uploader
  (YouTube Data API).
- Worker agent prompts live in launchpad/agents/*.py as PROMPT
  constants.
- Recent videos live in the DB (sqlite3 path:
  C:/Users/ritik/AppData/Local/launchpad/data/launchpad.db) — table
  `videos`, columns include script, title, description, tags,
  youtube_url, youtube_video_id, uploaded_at, etc.
- AI-content disclosure block in description is NON-NEGOTIABLE — never
  edit prompts to remove it. YouTube's 2026 synthetic-media policy
  requires it; removing it risks the channel.

DATA YOU CAN READ:
- Today is {today}. 7-day cutoff: {cutoff}.
- Logs and DB rows via sqlite3:
    python -c "import state; con=state.connect().__enter__(); [print(dict(r)) for r in con.execute('SELECT * FROM videos ORDER BY id DESC LIMIT 10')]"
- YouTube video stats (views, likes, comments) for any uploaded video
  with youtube_video_id, via:
    curl -s "https://www.googleapis.com/youtube/v3/videos?id=<VIDEO_ID>&key=..."
    (or use the existing oauth token in creds/youtube_token.json — but
    that requires a slightly different scope; for v1 you can use the
    YouTube public watch page HTML to read view counts.)
- The current agent prompts in launchpad/agents/screenplay.py,
  metadata.py, thumbnail.py.

YOUR THREE OUTPUTS:

1. WEEKLY REPORT — write to launchpad/meta_reports/{today}.md:
   - "Shipped this period": list of videos with view counts where
     available, titles, durations.
   - "Voice-over quality": is the scripting hooky? Too dry? Are the
     openings weak? Are the AI-disclosure lines awkward?
   - "Metadata quality": are titles getting clicks? Are tags reaching
     the right audiences? Is the description structure working?
   - "Thumbnail quality": readable at 320x180 (the small YouTube
     suggested-video size)? Consistent channel branding?
   - "Recommended changes": up to {max_edits} concrete prompt edits.
   - "Edits applied": filled in after you make them.

2. PROMPT EDITS — at most {max_edits} files in launchpad/agents/*.py.
   ONLY edit the PROMPT / CANDIDATES_PROMPT / RANK_AND_FINAL_PROMPT
   string constants. DO NOT touch:
   - orchestrator.py, recorder.py, tts.py, compositor.py, uploader.py,
     state.py, config.py, trigger.py, claude_cli.py
   - Function signatures, imports, control flow inside agents/*.py
   - The AI-content disclosure block in metadata.py (non-negotiable)

3. RECORD each edit in the prompt_versions table:
   python -c "import state; state.record_prompt_edit('screenplay', '<one-line diff summary>', '<rationale>')"

Each edit must be small, well-justified, and only after observing
real evidence in the DB / log / YouTube stats. If the period was clean
and no improvements are warranted, write the report saying so and apply
ZERO edits — restraint is correct behavior.

When done print on the LAST line:
COMMIT: opus meta review {today}
"""


def run_meta_review() -> str:
    today = datetime.now().date().isoformat()
    cutoff = (datetime.now(timezone.utc).astimezone()
              .replace(microsecond=0)).isoformat()
    out_dir = BASE_DIR / "meta_reports"
    out_dir.mkdir(exist_ok=True)

    state.log("INFO", "meta_improver",
              f"weekly Opus meta-review starting (model={META_MODEL})")
    output = claude_cli.call_claude(
        PROMPT.format(today=today, cutoff=cutoff,
                      max_edits=MAX_META_EDITS_PER_RUN),
        cwd=BASE_DIR,
        component="meta_improver",
        model=META_MODEL,
        allowed_tools=["Read", "Glob", "Grep", "Bash", "Write", "Edit"],
        timeout_s=2400,  # Opus is slower; 40 min ceiling
    )
    state.log("INFO", "meta_improver",
              f"weekly review complete ({len(output)} chars)")
    return str(out_dir / f"{today}.md")
