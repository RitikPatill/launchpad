"""
Shorts metadata agent. Dedicated Sonnet call for Shorts-specific title,
description, and hashtags.

Shorts metadata differs from long-form:
  - Title shorter (<=50 chars; YouTube's Shorts UI truncates aggressively)
  - The "#Shorts" tag in the title or first line of description is what
    routes the video to the Shorts shelf
  - Description should be SHORT (3-5 lines max) — the Shorts UI shows
    very little before "Show more"
  - Hashtags matter MORE than for long-form (they drive related-Short
    discovery)
  - AI-content disclosure still mandatory but in 1 short line at the end
"""
from __future__ import annotations

import claude_cli


PROMPT = """You are the SHORTS METADATA agent. Write YouTube Shorts
metadata: title, description, tags. Different rules from long-form.

PROJECT TITLE:    {title}
PROJECT PITCH:    {description}
REPO URL:         {repo_url}
LONG-FORM URL:    {long_form_url}
SCRIPT (the voice-over):
{script}

OUTPUT — return ONLY a JSON object:
{{
  "title": "<= 50 chars; should include #Shorts at the end OR mid-sentence>",
  "description": "<short, see structure below>",
  "tags": ["tag1", ... 8-12 tags, lowercase, can include hashes]
}}

TITLE RULES:
  - Max 50 chars (YouTube Shorts UI truncates).
  - Must include either "#Shorts" at the end OR be obviously vertical.
  - Lead with the hook, not the project name.
  - Examples that work:
      "I built an AI that ships my GitHub #Shorts"
      "This agent remembers years of context #Shorts"
  - Examples that don't:
      "MemGraph: A Personal AI Assistant with Knowledge Graph"  (too long, dry)
      "Cool AI project demo!"  (vague, no hook)

DESCRIPTION STRUCTURE (3-5 lines TOTAL, short):

Line 1: One-sentence hook (same energy as title).
Line 2: GitHub link: github.com/RitikPatill/<slug>
(blank)
Line 3: "Full walkthrough: <long_form_url>"
(blank)
Line 4: "AI disclosure: voice is AI-generated; code is real and open source."
(blank)
Line 5 (optional): A line of 3-5 hashtags: #AI #LLM #Agents

That's it. No marketing words. No emoji.

TAGS:
  - 8-12 tags
  - Mix specific (project-niche) and broad (#shorts, #ai, #llm)
  - Lowercase
  - Tags help related-Short discovery

CONSTRAINTS:
  - No emoji anywhere.
  - No "amazing", "powerful", "blazing", "robust", "wild".
"""


def write_shorts_metadata(*, title: str, description: str, repo_url: str,
                          long_form_url: str, script: str) -> dict:
    prompt = PROMPT.format(
        title=title, description=description,
        repo_url=repo_url, long_form_url=long_form_url,
        script=script[:2000],
    )
    raw = claude_cli.call_claude(
        prompt,
        component="shorts_metadata",
        allowed_tools=[],
        timeout_s=300,
    )
    return claude_cli.extract_json(raw)
