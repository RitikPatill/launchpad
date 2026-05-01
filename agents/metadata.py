"""
Metadata agent. Generates YouTube title, description, tags.

The video performance on YouTube is overwhelmingly driven by:
  1. Title — 70% of click-through-rate signal
  2. Thumbnail — also huge, but we use the first frame for now
  3. Description first 2 lines (the bit shown above "Show more")
  4. Tags — moderate signal for related-video surfacing
"""
from __future__ import annotations

import claude_cli


PROMPT = """You are writing YouTube metadata for a technical walkthrough
video about an AI agent project.

PROJECT TITLE: {title}
DESCRIPTION:   {description}
REPO URL:      {repo_url}
SCRIPT (the voice-over content):
{script}

OUTPUT — return ONLY a JSON object:
{{
  "title": "<= 60 chars, descriptive, no clickbait, includes project name>",
  "description": "<multi-line, see structure below>",
  "tags": ["tag1", "tag2", ... up to 12]
}}

DESCRIPTION STRUCTURE (multi-line plain text):
Line 1-2: One-sentence pitch + repo URL. (These are the lines visible
           before "Show more".)

(blank line)

Paragraph 2 (~3 sentences): What the project does, key technical choices.

(blank line)

Paragraph 3: "Built autonomously by my multi-agent system [autodev / agent-radar].
You can find the orchestrator at https://github.com/RitikPatill/autodev."

(blank line)

Section "Links":
- Repo: <repo_url>
- (if applicable) Live demo: <pages_url>

(blank line)

"AI disclosure: this video was scripted, narrated, and edited by an
autonomous agent pipeline I built. The voice you hear is AI-generated.
The code being shown is real and runnable."

CONSTRAINTS:
- Title under 60 chars (YouTube cuts off at ~70).
- No emoji. No marketing language.
- No fake urgency ("watch this NOW").
- Tags: lowercase, single words or short phrases. Mix project-specific
  (e.g. "knowledge-graph", "rag") with broad ("ai", "llm", "tutorial").
"""


def write_metadata(*, title: str, description: str, repo_url: str,
                   script: str) -> dict:
    prompt = PROMPT.format(
        title=title, description=description,
        repo_url=repo_url, script=script[:3000],
    )
    raw = claude_cli.call_claude(
        prompt,
        component="metadata",
        allowed_tools=[],
        timeout_s=300,
    )
    return claude_cli.extract_json(raw)
