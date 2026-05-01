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

  scene 1 (3-5s) — THE HOOK. A single sentence that makes the viewer
                   stop swiping. Examples of good hooks:
                     - "I built an AI that ships my GitHub portfolio
                        for me."
                     - "What if your side projects wrote themselves?"
                     - "This memory system lets agents remember years
                        of context."
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
