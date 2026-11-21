"""
Storyboard agent: turns a shipped project into a motion-graphics video plan
(long-form 16:9 or a vertical Short), grounded in the project's real code.

Two passes on the most capable model:
  1. WRITER  drafts the storyboard from the repo material + channel evidence.
  2. EDITOR  attacks the draft (hook, pacing, accuracy, visual variety) and
             returns the improved storyboard.
The result is validated/normalised by motion.validate before rendering.
"""
from __future__ import annotations

import json

import claude_cli
from agents import channel_insights, repo_context
from config import EFFORT_CREATIVE, MODEL_CREATIVE

HANDLE = "RitikPatill"

SCENE_CATALOG = """SCENE TYPES (the renderer animates each one; pick per beat, vary them):
- hook      {"headline": <=9 words, "emphasis": [1-2 words copied from the headline], "kicker": optional <=4 words}
- statement {"headline": <=8 words, "emphasis": [...], "sub": optional <=12 words}
- bullets   {"title": optional <=6 words, "items": 2-4 items of <=7 words, "style": "check"|"num"|"arrow"}
- code      {"filename": "...", "language": "python|ts|js|bash|go|...", "code": "<=14 lines", "highlight": [line numbers within this snippet], "caption": optional}
            The code must be copied verbatim from the SOURCE or README material (you may drop lines and put "..." on its own line). Never write new code.
- terminal  {"title": optional, "lines": [{"cmd": "..."}, {"out": "..."}]}
            Commands and outputs must come from the README/docs. Never invent output; if none is documented, show commands only.
- diagram   {"title": optional <=5 words, "nodes": [{"id", "label" <=3 words, "sub" optional <=4 words, "hot": optional true on the key node}] (3-7 nodes),
             "edges": [["from_id", "to_id"], ...]}   (laid out left-to-right / top-to-bottom automatically; keep it a clean flow)
- stat      {"title": optional, "items": [{"value": number, "prefix": optional, "suffix": optional, "label": <=4 words}] (1-3 items)}
            Only numbers that are stated in the material. No invented benchmarks.
- compare   {"title": optional, "left": {"title", "items": [2-4]}, "right": {"title", "items": [2-4]}}   (right = this project's approach)
- browser   {"image": "repo", "url": "github.com/<owner>/<repo>", "scroll_frac": 0.0-1.0}   (a real screenshot of the GitHub page, slowly scrolled)
- cta       {"headline": <=6 words, "url": "github.com/<owner>/<repo>", "sub": optional <=8 words}
Every scene: {"type", "voice_over": spoken text, "visual": {...}} plus, for long-form, "chapter": 2-4 word label."""

PLAYBOOK = """WHAT WORKS ON THIS CHANNEL (evidence from its own analytics, 92 uploads):
- Shorts drive ~75% of all views; the median Short is swiped away after ~13 s, long-form after ~26 s.
  The first 2 seconds decide everything. Something must change on screen every ~3 s in Shorts, ~6 s in long-form.
- Best hook shapes by measured retention:
  1. Your-situation, second person, then a turn: "Your prompt worked yesterday. Did it survive the model update?" (67 % viewed - channel best)
     "You see the final answer. You don't see the 47 tool calls that got there." (44 %)
  2. Claim + counter-claim: "Most agent benchmarks score the final answer. This one scores the trajectory."
  3. A concrete can-you question: "Can an LLM spot anomalies in a raw CSV?"
  4. "What if..." only with a physical, mimeable verb (watch, break, catch, crash, ship) - abstract verbs (grade, configure, evaluate) flopped.
- Hooks that lost: "This is X...", "I built...", "Today I'm...", "Let me show you...", category openers ("Every LLM app..."),
  three clipped sentences in a row, more than two technical nouns in the hook.
- Titles that won name a pain or a visual payoff ("Why Your LLM Calls the Wrong Model", "Agent Memory Architecture: Visual Walkthrough").
  Generic "How to Build X with Y" titles got 2-6 views.
- Viewers are developers and AI/ML hiring managers. Show the thing working and the clever part of the code; skip setup trivia."""

RULES_LONG = """FORMAT: long-form 16:9, 7-10 scenes, 230-340 spoken words total (~85-130 s).
Arc: hook (problem the viewer feels) -> why it is hard / what usually goes wrong -> the idea -> how it works (diagram) ->
the clever part in real code -> it running (terminal/browser) -> result/what you get -> cta.
Scene 1 is a hook scene; its voice_over is <=20 words, at most 2 sentences, and must not repeat the on-screen headline word for word.
Exactly one sentence somewhere after scene 2 says plainly that the project was built by an autonomous multi-agent system the
author designed (honest disclosure - it is also part of the story). Last scene is a cta pointing to the repo.
Voice: a sharp senior engineer talking to a peer. Concrete, specific, no hype words (amazing, powerful, robust, seamless, game-changer).
Give each scene a "chapter" label."""

RULES_SHORT = """FORMAT: vertical YouTube Short, 4-6 scenes, 70-105 spoken words total (~28-42 s). One idea only.
Scene 1 is a hook scene whose voice_over is <=12 words and lands the tension immediately - no context, no greeting.
Cut every sentence that does not raise or pay off that tension. Fast visual changes (a new scene every 4-7 s).
End on a short line that resolves the hook and makes a replay feel natural; the final scene may be a cta
("Full build on the channel" + repo url) but keep its voice_over <=6 words.
ANGLE for this Short: {angle}"""

WRITER = """You write motion-graphics videos for a developer's YouTube channel. Each video presents one open-source project
the author shipped. Your storyboard is rendered automatically (animated typography, real code, diagrams, terminals) and voiced
by a neural TTS voice, so write voice_over for the ear: short sentences, no markdown, no emoji, no URLs read aloud.
Write every number and version with digits (47, 3.5x, 90%, GPT-4o) - the voice reads digits naturally, and every number
is fact-checked against the repo material; any number that is not in the material will be cut from the video.

PROJECT
title: {title}
description: {description}
repo: {repo_url}

{format_rules}

{playbook}

LIVE CHANNEL NUMBERS (latest)
{insights}

{catalog}

REPO MATERIAL (the only source of truth for code, commands and numbers)
{material}

Return only a JSON object: {{"hook_shape": "<which shape you used>", "scenes": [ ... ]}}"""

EDITOR = """You are the channel's head of content: a ruthless editor who has watched thousands of developer Shorts and
explainers and knows exactly where viewers swipe away. Here is a draft storyboard for the project below.

PROJECT: {title} - {description} ({repo_url})

{format_rules}

{playbook}

{catalog}

REPO MATERIAL (truth source)
{material}

DRAFT
{draft}

Improve it. Specifically:
1. Hook: would a developer scrolling at speed stop? If not, rewrite it using the strongest shape for this project.
2. Every claim, code line, command, output and number must be supported by the repo material - fix or cut anything that is not.
3. Pacing: tighten every voice_over; respect the word budgets; each scene must earn its place.
4. Visual variety: avoid two similar scene types back to back; pick the visual that best shows what is being said.
5. Clarity: by the end a viewer must know what it does, why it is clever, and where to get it.
Return only the final storyboard as a JSON object in the same schema, plus "editor_notes": one sentence on what you changed."""


FIX = """This storyboard for a developer video contains numbers that are NOT supported by the project's repo material:
{claims}

Rewrite ONLY the voice_over and visual text that contain those numbers so that every number shown or spoken is supported
by the material below, or is removed and expressed qualitatively. Keep every other scene, field and word identical, and
keep the same JSON schema.

REPO MATERIAL (truth source)
{material}

STORYBOARD
{storyboard}

Return only the corrected storyboard as a JSON object."""


def fix_claims(sb: dict, claims: list[str], ctx: dict) -> dict:
    """One targeted rewrite pass for unsupported numbers (no tools)."""
    fixed = _call(FIX.format(claims=", ".join(claims), material=repo_context.as_prompt(ctx),
                             storyboard=json.dumps({"scenes": sb.get("scenes", [])}, indent=1)),
                  "storyboard_fix_claims")
    out = dict(sb)
    out["scenes"] = fixed["scenes"]
    return out


def _call(prompt: str, component: str) -> dict:
    raw = claude_cli.call_claude(prompt, component=component, model=MODEL_CREATIVE,
                                 effort=EFFORT_CREATIVE, allowed_tools=[], timeout_s=1200)
    data = claude_cli.extract_json(raw)
    if not isinstance(data, dict) or not data.get("scenes"):
        raise claude_cli.ClaudeError(f"{component}: no scenes in response")
    return data


def write_storyboard(*, kind: str, title: str, description: str, repo_url: str,
                     ctx: dict, angle: str = "") -> dict:
    """kind = "long" | "short". Returns the edited storyboard dict."""
    format_rules = RULES_LONG if kind == "long" else RULES_SHORT.format(
        angle=angle or "the single most surprising thing this project does")
    common = dict(title=title or "", description=description or "", repo_url=repo_url or "",
                  format_rules=format_rules, playbook=PLAYBOOK, catalog=SCENE_CATALOG,
                  material=repo_context.as_prompt(ctx))
    draft = _call(WRITER.format(insights=channel_insights.summary(), **common), f"storyboard_{kind}_draft")
    try:
        final = _call(EDITOR.format(draft=json.dumps(draft, indent=1), **common), f"storyboard_{kind}_edit")
    except Exception:
        final = draft  # editor pass is an improvement, never a single point of failure
    final["_draft_hook"] = (draft.get("scenes") or [{}])[0].get("voice_over", "")
    return final
