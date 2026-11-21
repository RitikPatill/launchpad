"""
Shorts screenplay agent. Dedicated Sonnet call for vertical 9:16
30-50s YouTube Shorts content.

Shorts are NOT just shortened long-form videos — they need fundamentally
different content design:
  - HOOK in first 2-3 seconds. If the viewer doesn't lock in by second 3,
    they swipe. The hook must be a question or a striking claim.
  - Vertical layout. The recorder uses 1080x1920 viewport, so wide
    horizontal scrolling is a non-starter — vertical scrolls only.
  - 5-8 second scenes. Each scene must deliver one concrete thing fast.
  - Silent-watch friendly. Many Shorts viewers watch without sound. The
    burned-in captions must carry the story by themselves.
  - No technical jargon density. Long-form viewers tolerate "vector
    embeddings"; Shorts viewers want "the AI remembers what you told it
    last week".
"""
from __future__ import annotations

import claude_cli


PROMPT = """You are the SHORTS SCREENPLAY agent. Write a YouTube Shorts
script (vertical 9:16, 30-50 seconds total) for an AI agent project.
Output the voice-over and the recorder actions in JSON.

PROJECT TITLE:    {title}
PITCH:            {description}
GITHUB REPO URL:  {repo_url}
LIVE DEMO URL:    {pages_url}

VIEWPORT IS VERTICAL (1080x1920). Browser actions must respect this:
  - Scrolls are vertical only. A scroll y=600 reveals one screenful.
  - Horizontal pages (GitHub repo) get cropped on the sides. PREFER the
    LIVE DEMO URL whenever it's available — Streamlit/Gradio apps render
    well at narrow widths.
  - Avoid clicking on small UI elements; they're hard to see in vertical.

VIDEO STRUCTURE — 4-6 scenes totaling 30-50 seconds:

  scene 1 (3-5s) — THE HOOK. A single short sentence (HARD LIMIT:
                   12 words or fewer — count them; v32 mcp-eval-studio
                   2026-08-11 shipped a 20-word Shorts-adjacent hook
                   and retained 19.57%, roughly a third of channel
                   median). Make the viewer stop swiping. PREFER
                   question-with-concrete-artifact framing —
                   empirically (channel video_analytics 2026-06-06)
                   this pattern produced the channel's highest Shorts
                   retention (v11 "Can an LLM Detect Time-Series
                   Anomalies from a CSV?" — 56.5% retention) while
                   generic "I built an AI that..." first-person hooks
                   (v8, v10, v12, v13 shorts) all retained <35%.

                   SCENE-1 ANTI-PATTERNS (2026-08-14 audit — two
                   consecutive Shorts flops in the same week). Do NOT
                   ship a scene-1 hook that matches either shape:

                   (a) CATEGORICAL-GENERIC THIRD-PERSON. Openers like
                       "Every LLM app hits the token wall eventually"
                       (v30 contextweaver — Shorts retention 9.60%,
                       channel-worst since v16 rubricon). The subject
                       is a category, not the viewer or a specific
                       instance the viewer cares about. Rewrite to
                       second-person ("Your app just hit the token
                       wall") or specific-first-instance ("This LLM
                       agent broke at 8k tokens").

                   (b) BUZZWORD-STACKED SECOND-PERSON. Openers like
                       "What if you could point a Claude agent at any
                       MCP server and get a scored, traceable eval in
                       seconds?" (v32 mcp-eval-studio — 20 words,
                       FIVE technical noun-phrases, 19.57% Shorts
                       retention). The framing is correct
                       (second-person question) but the noun density
                       is fatal — Shorts viewers cannot parse five
                       domain terms in one clause before swiping. Cap
                       yourself at TWO technical noun-phrases inside
                       scene 1; the rest belong in scene 2. Every
                       historical winner respects this: v11 (2 nouns
                       "LLM/CSV" → 56.5%), v27 (2 nouns
                       "prompt/regression" → 43.4%), v29 (2 nouns
                       "LLM/question" → 39.4%). Every recent loser
                       breaks it.

                   NEW EVIDENCE (channel video_analytics 2026-06-12):
                   hook LENGTH matters as much as hook shape. Compare
                   scene-1 word counts vs retention:
                     - v11 (9 words, "Can an LLM Detect Time-Series
                       Anomalies from a CSV?") — 56.49% retention.
                     - v13 (~15 words, "Most eval frameworks score what
                       an agent returns. TrajectoryLab scores how it got
                       there.") — 72.83% retention (channel best ever).
                     - v15 authorlens (~30 words, two-clause compound
                       question with technical follow-up) — 31.05%.
                     - v16 rubricon (~26 words, claim + immediate
                       technical extension + "Here's how that works")
                       — 17.82% (channel worst). Viewers swiped before
                       the hook had finished reading.
                   Therefore: scene 1 must be ONE short sentence ONLY.
                   Do NOT append a second clause or a "here's how" /
                   "the way it works" follow-up inside scene 1. The
                   technical extension belongs in scene 2, AFTER the
                   viewer has committed.

                   Examples of good hooks, ordered by empirical strength:
                     - "Can an LLM detect anomalies in a raw CSV?"
                       (9 words, question + concrete artifact — STRONGEST)
                     - "What if your agent could grade its own code?"
                       (9 words, question + concrete capability)
                     - "This memory system lets agents remember years
                        of context."  (10 words, claim + concrete payoff)
                     - "I built an AI that ships my GitHub portfolio
                        for me."  (first-person — use as last resort)
                   Always name the concrete artifact (a CSV, a webpage,
                   a graph, a repo) — abstract hooks like "an AI that
                   helps you" underperform every time.
                   Recorder: goto the demo URL (or repo if no demo),
                   wait 1s on first frame.

  scene 2-3 (8-12s each) — THE PAYOFF. Show what the project actually
                   does, in concrete visuals. If LIVE DEMO URL is set,
                   navigate there and demonstrate. Vertical scrolls
                   only. Voice-over names the technical interesting bit
                   ("typed knowledge graph", "retrieval-augmented
                   reasoning", etc.) but in plain English.

  scene 4 (5-8s) — THE PROOF. Quick cut to the GitHub repo, showing
                   it's open source and real. Voice: "Open source on
                   GitHub at RitikPatill slash project-name."

  final scene (3-5s) — THE CTA. "Full walkthrough on my channel. Built
                   autonomously by my multi-agent system." Recorder
                   ends on the demo URL or repo.

VOICE-OVER STYLE:
  - First-person ("I built", "I designed").
  - Casual but not glib. No "guys" or "let's gooo".
  - Strong verbs in present tense.
  - No emoji-equivalent vocal cues.
  - Disclose AI involvement in the FINAL scene only — quick mention,
    not a paragraph.

OUTPUT — return ONLY a JSON object:
{{
  "scenes": [
    {{
      "voice_over": "<the spoken text for this scene>",
      "actions": [
        {{"type": "goto", "url": "..."}},
        {{"type": "wait", "seconds": 2}},
        {{"type": "scroll", "y": 600, "duration_ms": 1500}}
      ]
    }}
  ],
  "total_duration_estimate_seconds": 40
}}

ACTION TYPES (same as long-form recorder):
  - {{"type": "goto",   "url": "<URL>"}}
  - {{"type": "wait",   "seconds": <number>}}
  - {{"type": "scroll", "y": <pixels>, "duration_ms": <ms>}}
  - {{"type": "click",  "selector": "<css>"}}

CRITICAL: total estimated duration 30-50s. Anything over 58s WILL be
rejected as a Short by YouTube and routed to long-form. Anything under
30s feels too thin to retain attention.
"""


def write_shorts_screenplay(*, title: str, description: str, repo_url: str,
                            pages_url: str | None) -> dict:
    prompt = PROMPT.format(
        title=title, description=description, repo_url=repo_url,
        pages_url=pages_url or "(none — improvise hook from repo only, but try harder)",
    )
    raw = claude_cli.call_claude(
        prompt,
        component="shorts_screenplay",
        allowed_tools=[],
        timeout_s=600,
    )
    return claude_cli.extract_json(raw)
