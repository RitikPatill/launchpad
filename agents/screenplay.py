"""
Screenplay agent.

Given a project (title + description + repo URL + optional pages URL),
produces a "screenplay" — the voice-over script plus the timed list of
recorder actions (open URL, scroll to position, click, etc.). The
recorder executes this script step-by-step while reading the voice-over.

Output JSON:
  {
    "scenes": [
      {
        "voice_over": "<one short paragraph, ~10-15 seconds spoken>",
        "actions": [
          {"type": "goto",   "url": "https://github.com/RitikPatill/foo"},
          {"type": "wait",   "seconds": 2},
          {"type": "scroll", "y": 600, "duration_ms": 3000},
          {"type": "wait",   "seconds": 1}
        ]
      },
      ... 4-6 scenes total
    ],
    "total_duration_estimate_seconds": <int>
  }

The script feels like a human dev introducing their project — dry,
technical, no marketing language, no emoji equivalents in speech ("um,
amazing!", etc.).
"""
from __future__ import annotations

import claude_cli


PROMPT = """You are the SCREENPLAY agent. Write a 60-90 second voice-over
script for a YouTube walkthrough of a personal AI agent project,
together with the timed list of browser actions the recorder will
execute.

PROJECT TITLE: {title}
DESCRIPTION:   {description}
REPO URL:      {repo_url}
PAGES URL:     {pages_url}

VIDEO STRUCTURE — 4-6 scenes:
  scene 1 (10-15s) — open the repo URL, hook with what this project is
                     and why it matters. Recorder: goto repo URL, wait,
                     small scroll to show README header.
  scene 2 (15-20s) — scroll through the README features/architecture
                     section. Voice explains the key technical choices.
  scene 3 (10-15s) — scroll to the Quickstart section, narrate how to
                     run it.
  scene 4 (10-15s, OPTIONAL — only if PAGES URL is present and looks
                     like a deployed demo URL): goto pages_url, narrate
                     "and here's it running live". Wait 3-4s on the
                     loaded page.
  final scene (5-10s) — back to repo, wrap up, mention author handle
                     (RitikPatill), close.

VOICE-OVER STYLE:
- Dry, technical, first-person ("I built", "I designed").
- No marketing words: no "amazing", "powerful", "robust", "blazing".
- No emoji-equivalent vocal cues ("wow", "awesome").
- Disclose AI involvement once: ONE sentence somewhere mentioning the
  project was built by an autonomous multi-agent system you designed.
- Speak naturally. Imagine a developer in 2026 narrating their own work.

ACTION TYPES (recorder syntax):
- {{"type": "goto",   "url": "<URL>"}}                       — navigate
- {{"type": "wait",   "seconds": <number>}}                  — hold the frame
- {{"type": "scroll", "y": <pixels>, "duration_ms": <ms>}}   — smooth scroll
- {{"type": "click",  "selector": "<css>"}}                  — click element

KEEP IT TIGHT: total estimated duration must be 60-90 seconds.

OUTPUT — return ONLY a JSON object, no preamble:
{{
  "scenes": [
    {{
      "voice_over": "...",
      "actions": [{{"type": "goto", "url": "..."}}]
    }}
  ],
  "total_duration_estimate_seconds": 75
}}
"""


def write_screenplay(*, title: str, description: str, repo_url: str,
                     pages_url: str | None) -> dict:
    prompt = PROMPT.format(
        title=title, description=description,
        repo_url=repo_url,
        pages_url=pages_url or "(none — skip the live-demo scene)",
    )
    raw = claude_cli.call_claude(
        prompt,
        component="screenplay",
        allowed_tools=[],
        timeout_s=600,
    )
    return claude_cli.extract_json(raw)
