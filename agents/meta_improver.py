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
from config import BASE_DIR, MODEL_CREATIVE


META_MODEL = MODEL_CREATIVE  # most capable model (was claude-opus-4-7)
MAX_META_EDITS_PER_RUN = 2


PROMPT = """You are the META-IMPROVER for launchpad, a multi-agent system
that turns the open-source projects shipped on Ritik's GitHub into YouTube
videos and Shorts. Your job: make the output genuinely better at earning
views, retention and subscribers, based on evidence.

You run on the most capable model; use the depth, but be disciplined:
edit only when the numbers support it.

CONTEXT (pipeline since 2026-11-20, "motion engine"):
- Per video: agents/storyboard.py (writer + editor passes) -> motion/
  validate (integrity guard) -> agents/packaging.py (title, description,
  thumbnail concept, German localisation) -> tts.py voice chain ->
  motion/ renderer (animated scenes, captions) -> thumbnail -> upload.
- Each video also gets a Short, plus extra Shorts from other angles and
  for the back catalogue (table `extra_shorts`).
- The live channel learnings that every writer reads are the PLAYBOOK
  string in agents/storyboard.py. Keep it short and evidence-based:
  replace stale lines rather than appending rules.
- The classic prompts (screenplay.py, metadata.py, shorts_screenplay.py,
  shorts_metadata.py) are only a fallback path now; do not spend edits
  there unless a fallback was actually used this period.
- Recent videos live in the DB (sqlite3 path:
  C:/Users/ritik/AppData/Local/launchpad/data/launchpad.db) — table
  `videos`, columns include script, title, description, tags,
  youtube_url, youtube_video_id, uploaded_at, etc.
- AI-content disclosure block in description is NON-NEGOTIABLE — never
  edit prompts to remove it. YouTube's 2026 synthetic-media policy
  requires it; removing it risks the channel.

DATA (collected for you; you have NO shell and cannot query anything):
- Today is {today}.
{data}
- You may Read the files in agents/ and meta_reports/.

YOUR THREE OUTPUTS:

1. WEEKLY REPORT — return it (markdown) as your FINAL message; it is saved
   for you to meta_reports/{today}.md. Do not try to write the file.

   Section A — SHIPPED THIS PERIOD: list of videos with view counts
     where available, titles, durations.

   Section B — CONTENT QUALITY:
     * Voice-over: is the scripting hooky? Too dry? Are openings weak?
       Are AI-disclosure lines awkward?
     * Metadata: are titles getting clicks (high views, good like-ratio)?
       Are tags reaching the right audiences?
     * Thumbnail: readable at 320x180 (small YouTube suggested-video)?
       Consistent channel branding?
     * Shorts vs long-form: which is performing better? Are Shorts
       getting the algorithmic boost we expected?

   Section C — **CROSS-SYSTEM INTEGRATION AUDIT** (NEW — be paranoid here,
     these are the gaps the user shouldn't have to spot for me):
     * Are completed videos linked back from the SOURCE REPO's README?
       Query: every videos row with status='uploaded' AND
       source_orchestrator IN ('autodev','agent-radar') should also
       have status='embedded' shortly after. If a video has been
       'uploaded' for >24h without becoming 'embedded', the embedding
       step is broken — flag it.
     * For every project in autodev/agent-radar with status='done',
       does a corresponding videos row exist? If not, why didn't the
       trigger pick it up?
     * Are there marketing drafts (autodev/drafts/, agent-radar/drafts/)
       sitting unused for >7 days? The user has to manually post these
       to LinkedIn — flag as "user reminder needed" in the report.
     * For projects detected as Streamlit/Gradio, did HF Spaces deploy
       succeed? Any 'failed_terminal' or stuck 'deploying' videos?
     * Do the YouTube videos actually link to the GitHub repo and HF
       Space in their descriptions?
     * Is the orchestrator's tick log healthy or showing repeated
       errors / rate-limit pauses?

   Section D — RECOMMENDED CHANGES: up to {max_edits} concrete prompt
     edits backed by evidence from B or C above.

   Section E — EDITS APPLIED: filled in after you make them.

   Section F — **CRITICAL GAPS REQUIRING NEW CODE** (NEW): if you
     identify a workflow gap that needs a NEW module or NEW orchestrator
     step (not just a prompt tweak), describe it here in detail —
     the human will see this in the next session and either implement
     it or ask Claude Code to. Examples of valid Section F entries:
       - "Auto-detect when a project's HF Space goes 503 stale and
          trigger a rebuild"
       - "Add a screenshot generator that snapshots the deployed app
          for the marketing drafts"
       - "When YouTube engagement is low, regenerate thumbnail and
          re-upload via thumbnails().set"
     This is your way to escalate beyond your prompt-edit-only sandbox.

2. PROMPT EDITS — at most {max_edits} string constants in total.
   Editable: PLAYBOOK, RULES_LONG, RULES_SHORT in agents/storyboard.py;
   LONG and SHORT in agents/packaging.py; and (only if the fallback path
   ran) the PROMPT constants of the classic agents. Anchor every edit to
   stats evidence ("Shorts opening on X held 2x longer"). Prefer
   replacing a stale line over adding a new rule; keep PLAYBOOK under
   ~40 lines.
   DO NOT touch:
   - anything outside agents/: orchestrator.py, motion/, motion_pipeline.py,
     extra_shorts.py, tts.py, tts_worker.py, alerts.py, nowindow.py,
     uploader.py, monitor.py, state.py, config.py, trigger.py, claude_cli.py
   - in agents/: function code, signatures, imports, validation logic,
     SCENE_CATALOG (the renderer depends on its exact schema)
   - the honesty rules: the AI-disclosure sentence, "never invent code,
     output or numbers", containsSyntheticMedia

3. Edits are recorded automatically from the git diff of agents/; put the
   rationale for each edit in Section E of the report.

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
    before = _agents_diff()
    output = claude_cli.call_claude(
        PROMPT.format(today=today, cutoff=cutoff, data=_collect_data(),
                      max_edits=MAX_META_EDITS_PER_RUN),
        cwd=BASE_DIR,
        component="meta_improver",
        model=META_MODEL,
        # HARD scope, enforced by Claude Code permissions (not by prompt):
        # only Read/Edit/Write exist, edits are allowed on the six prompt
        # files only, writes only into meta_reports/. No shell, no MCP, no
        # permission bypass - in headless mode anything else is denied.
        allowed_tools=EDITABLE_RULES,
        skip_permissions=False,
        extra_args=["--tools", "Read,Edit", "--strict-mcp-config", "--restricted"],
        timeout_s=2400,
    )
    report = out_dir / f"{today}.md"
    report.write_text(str(output).strip() + "\n", encoding="utf-8")
    after = _agents_diff()
    if after and after != before:
        if _smoke_ok():
            state.record_prompt_edit("meta_improver", after[:6000],
                                     f"weekly review {today}; rationale in meta_reports/{today}.md")
        else:
            # A broken prompt edit must never crash-loop the pipeline.
            import subprocess
            subprocess.run(["git", "-C", str(BASE_DIR), "checkout", "--", "agents"], capture_output=True)
            import alerts
            alerts.notify("meta-edit-reverted", "Weekly self-improvement edit reverted",
                          "the prompt edit broke an import/template smoke test; reverted automatically",
                          level="ERROR")
    state.log("INFO", "meta_improver",
              f"weekly review complete ({len(output)} chars)")
    return str(out_dir / f"{today}.md")


EDITABLE_RULES = [
    "Read(agents/**)", "Read(meta_reports/**)",
    "Edit(agents/storyboard.py)", "Edit(agents/packaging.py)",
    "Edit(agents/screenplay.py)", "Edit(agents/metadata.py)",
    "Edit(agents/shorts_screenplay.py)", "Edit(agents/shorts_metadata.py)",
    "Edit(meta_reports/**)",   # Edit rules govern Write too
]


def _smoke_ok() -> bool:
    """Import every editable agent module in a FRESH interpreter and render
    every prompt template with dummy values."""
    import subprocess
    import sys
    code = (
        "import sys; sys.path.insert(0, '.');"
        "import agents.storyboard as s, agents.packaging as p;"
        "import agents.screenplay, agents.metadata, agents.shorts_screenplay, agents.shorts_metadata;"
        "kw = dict(title='t', description='d', repo_url='r', format_rules='f', playbook='p', catalog='c', material='m', insights='i');"
        "s.WRITER.format(**kw); s.EDITOR.format(draft='x', **{k: v for k, v in kw.items() if k != 'insights'});"
        "s.RULES_SHORT.format(angle='a'); s.FIX.format(claims='c', material='m', storyboard='s');"
        "p.LONG.format(title='t', description='d', repo_url='r', script='s', playbook='p', insights='i');"
        "p.SHORT.format(title='t', description='d', script='s'); p.TRANSLATE.format(lines='[]')"
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(BASE_DIR), capture_output=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


def _agents_diff() -> str:
    import subprocess
    try:
        p = subprocess.run(["git", "-C", str(BASE_DIR), "diff", "--", "agents"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return p.stdout.strip()
    except Exception:
        return ""


def _collect_data() -> str:
    """Everything the reviewer used to fetch with a shell, pre-computed."""
    from agents import channel_insights
    parts = ["CHANNEL PERFORMANCE (latest stats):\n" + channel_insights.summary(limit=8)]
    with state.connect() as con:
        rows = list(con.execute(
            "SELECT id, status, title, tts_engine, uploaded_at, script FROM videos ORDER BY id DESC LIMIT 12"))
        vids = []
        for r in rows:
            try:
                sc = json.loads(r["script"] or "{}")
            except json.JSONDecodeError:
                sc = {}
            engine = sc.get("engine", "classic")
            scenes = (sc.get("storyboard") or {}).get("scenes") or sc.get("scenes") or []
            hook = (scenes[0].get("voice_over", "") if scenes else "")[:140]
            vids.append(f"  #{r['id']} [{r['status']}] {engine} voice={r['tts_engine']} "
                        f"uploaded={str(r['uploaded_at'])[:10]} title={r['title']!r} hook={hook!r}")
        parts.append("RECENT VIDEOS:\n" + "\n".join(vids))
        errs = list(con.execute(
            "SELECT level, component, substr(message, 1, 90) AS m, COUNT(*) AS n, MAX(ts) AS last FROM log "
            "WHERE level IN ('WARN','ERROR') AND ts >= datetime('now', '-7 days') "
            "GROUP BY level, component, m ORDER BY n DESC LIMIT 25"))
        parts.append("WARN/ERROR LOG, last 7 days (count | last seen):\n" + "\n".join(
            f"  {e['n']:>4} | {str(e['last'])[:16]} | {e['level']} {e['component']}: {e['m']}" for e in errs))
        try:
            ex = list(con.execute("SELECT status, COUNT(*) AS n FROM extra_shorts GROUP BY status"))
            parts.append("EXTRA SHORTS by status: " + ", ".join(f"{e['status']}={e['n']}" for e in ex))
        except Exception:
            pass
        pv = list(con.execute("SELECT applied_at, agent_name, substr(rationale, 1, 120) AS r "
                              "FROM prompt_versions ORDER BY id DESC LIMIT 8"))
        parts.append("RECENT PROMPT EDITS:\n" + "\n".join(f"  {x['applied_at'][:10]} {x['agent_name']}: {x['r']}" for x in pv))
    return "\n\n".join(parts)
