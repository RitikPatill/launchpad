"""
Metadata agent. Generates YouTube title, description, tags.

Two-pass strategy:
  Pass 1: produce 5 candidate titles in different styles (descriptive,
          curiosity, how-to, question, technical).
  Pass 2: rank them against a CTR rubric and pick the winner.

This A/B-style generation produces more compelling titles than asking
for "the best title" in one shot. Single-shot tends to default to dry
descriptive style which has poor click-through.

Performance signals on YouTube:
  - Title — ~70% of CTR
  - Thumbnail — also huge (we generate via PIL composite)
  - First two lines of description — show above "Show more"
  - Tags — moderate signal for related-video surfacing

Description always includes the AI-content disclosure block at the end.
This is BOTH legally required by YouTube (synthetic media flag) AND a
trust signal — viewers and the algorithm both reward upfront honesty.
"""
from __future__ import annotations

import claude_cli


CANDIDATES_PROMPT = """Produce 5 distinct YouTube title candidates for
this technical walkthrough video. Each must be under 60 characters,
include the project name or a clear topical hook, and avoid emoji and
clickbait language.

PROJECT TITLE: {title}
DESCRIPTION:   {description}

Generate one title in each of these styles:
  1. Plain descriptive — what it is
  2. Curiosity gap — implies a non-obvious choice or result
  3. How-to / tutorial framing
  4. Question framing
  5. Technical handle — uses one keyword recruiters search

OUTPUT — return ONLY a JSON array of 5 strings, no other text:
["...", "...", "...", "...", "..."]
"""


RANK_AND_FINAL_PROMPT = """You are picking the FINAL YouTube metadata
for a technical walkthrough video.

PROJECT TITLE: {title}
DESCRIPTION:   {description}
REPO URL:      {repo_url}
SCRIPT:
{script}

CANDIDATE TITLES (pick or refine the best one):
{candidates}

CTR RUBRIC — score each candidate mentally on:
  - Concrete > abstract (specific feature beats vague pitch)
  - Searchable keyword present (someone looking for this would type X)
  - Honest > clickbait
  - Under 60 chars (YouTube truncates display)

OUTPUT — return ONLY a JSON object:
{{
  "title": "<the winning title, max 60 chars; you may refine slightly>",
  "description": "<see structure below>",
  "tags": ["tag1", "tag2", ... 8-12 tags]
}}

DESCRIPTION STRUCTURE:
Line 1-2: One-sentence pitch + repo URL. (These are the lines visible
           before "Show more" — make them count.)

(blank line)

~3 sentences: what the project does, key technical choice, why it matters.

(blank line)

"Built autonomously by my multi-agent system at
https://github.com/RitikPatill/autodev — the orchestrator code is open
source."

(blank line)

"Repo: <repo_url>"

(blank line)

"AI disclosure: this video was scripted, narrated, and edited by an
autonomous agent pipeline I designed. The voice you hear is AI-generated
(Microsoft Edge TTS). The code being demonstrated is real and runnable.
This disclosure satisfies YouTube's 2026 synthetic-media policy."

CONSTRAINTS:
- No emoji anywhere.
- No marketing words ("amazing", "powerful", "blazing", "robust").
- Tags: lowercase, single words or short phrases.
"""


def _generate_candidates(title: str, description: str) -> list[str]:
    raw = claude_cli.call_claude(
        CANDIDATES_PROMPT.format(title=title, description=description),
        component="metadata_candidates",
        allowed_tools=[],
        timeout_s=180,
    )
    parsed = claude_cli.extract_json(raw)
    if isinstance(parsed, list):
        return [str(t) for t in parsed][:5]
    if isinstance(parsed, dict) and "titles" in parsed:
        return [str(t) for t in parsed["titles"]][:5]
    return [title]


def write_metadata(*, title: str, description: str, repo_url: str,
                   script: str) -> dict:
    candidates = _generate_candidates(title, description)
    candidates_block = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(candidates))

    raw = claude_cli.call_claude(
        RANK_AND_FINAL_PROMPT.format(
            title=title, description=description,
            repo_url=repo_url, script=script[:3000],
            candidates=candidates_block,
        ),
        component="metadata",
        allowed_tools=[],
        timeout_s=300,
    )
    return claude_cli.extract_json(raw)
