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
  6. Opinion / Comparison — "Why I chose X over Y", "X vs Y", or
     "The problem with X". USE THIS STYLE ONLY when the project embodies
     a defensible technical choice against a widely-known alternative
     (Knowledge Graph vs RAG, YAML vs JSON, agent vs single-shot LLM,
     local vs hosted, etc.). If no such comparison exists, output a
     second Question-style variant instead so the array still has 6
     entries.

OUTPUT — return ONLY a JSON array of 6 strings, no other text:
["...", "...", "...", "...", "...", "..."]
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
  - PREFER titles that include one of these canonical AI-engineering
    search terms — viewers actively search for them and the channel's
    own stats confirm they pull far more traffic than abstract phrasing:
      RAG, LLM, Memory, Embeddings, Multi-Agent, Self-Improving,
      Self-Correcting, Knowledge Graph, Vector, Retrieval, Eval,
      Tool Use, Agent Architecture.
    Empirical anchor (from the launchpad channel's own video_analytics
    table as of 2026-05-25): titles containing one of these keywords
    account for ~78% of all channel views; the single best-performing
    title ("How to Build a Self-Improving Code Agent With RAG") earned
    ~26x more views than the same project re-shipped as "How to Build
    an LLM Agent With a Growing Skill Library". Treat this as
    observation, not guess.
  - AVOID leading with "<ProjectName>: <description>". Project-name-led
    titles on this channel ("WebAct: Self-Correcting Browser Agent in
    Python", "RubricLab: LLM-as-Judge Scoring for Agent Evals") sit at
    the bottom of the view distribution. Lead with the value, technique,
    or canonical keyword; the project name can come later in the title
    or only in the description.
  - OPINION / COMPARISON FRAME — soft preference (added 2026-09-01
    based on v36 codegraph-agent). When the project embodies a
    defensible technical choice against a widely-known alternative,
    prefer the "Why I chose X over Y" / "X vs Y" / "The problem with
    X" frame over the equivalent how-to. Empirical anchor: v36 "Why I
    Used a Knowledge Graph Instead of RAG for Code Search" is the
    channel's first opinion-framed title and retained 51.95 % long-form
    (highest of the last 5 uploads); v35 "How to Grade Your Prompts
    with an LLM-as-Judge Eval" (how-to frame on a project that had a
    clear comparison available — eval-as-code vs vibes-checking) drew
    1 long view. Sample size is 1 winner, so this is a soft preference,
    not a hard rule: if the project has NO honest opposing alternative
    (a pure utility, a novel technique with no incumbent), do NOT
    manufacture a comparison — fall back to how-to or question. Honest
    > clickbait still overrides.
  - TITLE NOUN-DENSITY CAP + STACKED-TOOL-LIST BAN (HARD RULE — added
    2026-08-20 based on v33 + v34 back-to-back regression). A "concept-
    noun" here = an acronym, a domain term, a proper tool/model name,
    or a jargon compound (e.g. "LLM", "CLIP", "DCT", "HuggingFace",
    "eval harness", "source grounding", "Pydantic schema"). Cap: at
    most FOUR concept-nouns in the final title. Additionally, DO NOT
    ship a title in the shape "<Verb> <N1> with <N2>, <N3>, and <N4>"
    (or any comma-separated tool enumeration in the second half of the
    title). This construction is the title-side analogue of the scene-1
    buzzword-stacking failure mode codified 2026-08-14 for screenplays.

    Anti-examples from the launchpad channel's own video_analytics
    (as of 2026-08-20):
      - v33 "Detect AI Images with CLIP Embeddings, DCT, and
        HuggingFace" — 5 concept-nouns, comma-tool-list → 2 long views.
      - v34 "Typed LLM Extraction with Source Grounding and Evals" —
        5+ concept-nouns, near-tool-list → 5 long views, 15.0 %
        retention (channel-worst long-form of the last 8 uploads).

    Positive comparators from the same table:
      - v22 "LLM-as-Judge Prompt Eval and Versioning with PromptOps"
        — 4 concept-nouns, natural syntax → 34 long views (winner).
      - v26 "How to Build a Multi-Agent Code Review Pipeline" — 3
        concept-nouns, How-to framing → 19 long views.
      - v25 "How to Build an LLM-as-Judge Eval Pipeline from Scratch"
        — 3 concept-nouns → 8 long views.

    Every historical view-winner (≥ 8 long-form views) respected ≤ 4
    concept-nouns and avoided the tool-list construction; every
    bottom-quartile loser stacked ≥ 5 or used the "with X, Y, and Z"
    enumeration. If a candidate title needs 5+ nouns to be accurate,
    the candidate is too technical for the *title* — move the extra
    tool names into the description body (first 2 lines) where they
    still catch search hits without visual overload.

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
(synthetic text-to-speech). The code being demonstrated is real and runnable.
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
