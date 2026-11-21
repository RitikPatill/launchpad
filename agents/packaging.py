"""
Packaging agent: title, description, tags, thumbnail concept and German
localisation for a finished storyboard. Most capable model - titles and
thumbnails decide whether anyone clicks at all.
"""
from __future__ import annotations

import json

import claude_cli
from agents import channel_insights
from agents.storyboard import PLAYBOOK
from config import EFFORT_CREATIVE, MODEL_CREATIVE, MODEL_STANDARD

LONG = """You package YouTube videos for a developer channel (audience: developers and AI/ML hiring managers, largely in
Germany/EU, content in English). Package this finished video.

PROJECT: {title}
DESCRIPTION: {description}
REPO: {repo_url}

FINAL SCRIPT (scene by scene):
{script}

{playbook}

LIVE CHANNEL NUMBERS
{insights}

Rules:
- Title <= 62 characters. Name the viewer's pain or the visual payoff, plus one searchable term (LLM, RAG, AI agent, eval, ...).
  No clickbait the video does not deliver, no ALL CAPS, no emoji, never the pattern "How to Build X with Y".
- description_intro: 2 sentences; the first 150 characters must work alone in search results.
- description_body: 3-6 short lines on what it does and how (plain text, may use "- " bullets).
- thumbnail.text: 2-4 punchy words that complement (not repeat) the title; thumbnail.emphasis: one of those words.
  thumbnail.visual_scene: the 0-based index of the scene whose visual best sells the video (prefer a diagram or code scene).
- tags: 10-15 specific search tags. hashtags: 3 (without spaces).
- de: natural German (not literal) title (<= 62 chars), description_intro and description_body for German viewers.
- pinned_comment: one genuine question that invites developers to reply.

Return only JSON:
{{"title": "", "title_alternatives": ["", "", ""], "description_intro": "", "description_body": "", "tags": [],
  "hashtags": [], "thumbnail": {{"text": "", "emphasis": "", "visual_scene": 0}},
  "de": {{"title": "", "description_intro": "", "description_body": ""}}, "pinned_comment": ""}}"""

SHORT = """Package this YouTube Short for a developer channel (audience: developers and AI/ML hiring managers in Germany/EU).

PROJECT: {title} - {description}
SHORT SCRIPT: {script}

Rules: title <= 55 characters, written like the hook (tension or payoff), one searchable term, no clickbait the Short does
not deliver, no emoji, no ALL CAPS. description: 1-2 sentences. tags: 8-12. de: natural German title (<= 55 chars) and
description.
Return only JSON: {{"title": "", "description": "", "tags": [], "de": {{"title": "", "description": ""}}}}"""

TRANSLATE = """Translate these English video caption lines into natural, concise German (as a native German developer would
say it; keep technical terms like LLM, RAG, prompt, API in English). Keep the same number of items, same order.
Return only a JSON array of strings.

{lines}"""


AI_DISCLOSURE = {
    "en": "AI disclosure: the narration is a synthetic text-to-speech voice and the visuals are generated "
          "animations. Code and commands shown are taken from the project's public repository.",
    "de": "KI-Hinweis: Die Erzählstimme ist synthetisch (Text-to-Speech) und die Visuals sind generierte "
          "Animationen. Gezeigter Code und Befehle stammen aus dem öffentlichen Repository des Projekts.",
}


def _unwrap(obj):
    """Models sometimes wrap the answer ({"packaging": {...}} or [{...}]);
    return the dict that actually carries a title."""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = obj[0]
    if isinstance(obj, dict) and "title" not in obj:
        for val in obj.values():
            if isinstance(val, dict) and "title" in val:
                return val
    return obj


def _script_text(sb: dict) -> str:
    return "\n".join(f"[{i}] ({s['type']}) {s.get('voice_over', '')}" for i, s in enumerate(sb.get("scenes", [])))


def package_long(*, title: str, description: str, repo_url: str, storyboard: dict) -> dict:
    raw = claude_cli.call_claude(
        LONG.format(title=title, description=description, repo_url=repo_url,
                    script=_script_text(storyboard), playbook=PLAYBOOK,
                    insights=channel_insights.summary()),
        component="packaging_long", model=MODEL_CREATIVE, effort=EFFORT_CREATIVE,
        allowed_tools=[], timeout_s=900)
    pk = _unwrap(claude_cli.extract_json(raw))
    if not isinstance(pk, dict) or not pk.get("title"):
        raise claude_cli.ClaudeError("packaging_long: no title")
    pk["title"] = str(pk["title"]).strip()[:95]
    pk.setdefault("tags", [])
    pk.setdefault("hashtags", [])
    pk.setdefault("thumbnail", {})
    pk.setdefault("de", {})
    return pk


def package_short(*, title: str, description: str, storyboard: dict) -> dict:
    raw = claude_cli.call_claude(
        SHORT.format(title=title, description=description, script=_script_text(storyboard)),
        component="packaging_short", model=MODEL_CREATIVE, effort=EFFORT_CREATIVE,
        allowed_tools=[], timeout_s=600)
    pk = _unwrap(claude_cli.extract_json(raw))
    if not isinstance(pk, dict) or not pk.get("title"):
        raise claude_cli.ClaudeError("packaging_short: no title")
    pk["title"] = str(pk["title"]).strip()[:90]
    return pk


def build_description(pk: dict, *, chapters: str, repo_url: str, lang: str = "en",
                      long_form_url: str = "") -> str:
    """Assemble the final YouTube description (en or de)."""
    src = pk if lang == "en" else {**pk, **(pk.get("de") or {})}

    def clean(s: str) -> str:   # YouTube rejects < and > in descriptions
        return (s or "").replace("<", "‹").replace(">", "›").strip()

    intro = clean(src.get("description_intro"))
    body = clean(src.get("description_body"))
    # Budget: trim the BODY (then the intro) - never the chapters, the links
    # or the disclosure at the end.
    while len((intro + body).encode("utf-8")) > 3200 and body:
        body = body[:max(0, len(body) - 200)].rstrip()
    intro = intro[:1500]
    parts = [p for p in (intro, body) if p]
    if chapters:
        parts.append(("Kapitel" if lang == "de" else "Chapters") + "\n" + chapters)
    parts.append(("Code (Open Source): " if lang == "de" else "Code (open source): ") + repo_url)
    if long_form_url:
        parts.append(("Ganzes Video: " if lang == "de" else "Full video: ") + long_form_url)
    parts.append("Gebaut und veröffentlicht von einem autonomen Multi-Agenten-System, das ich entworfen habe."
                 if lang == "de" else
                 "Built and shipped by an autonomous multi-agent system I designed.")
    tags = " ".join("#" + h.lstrip("#").replace(" ", "") for h in (pk.get("hashtags") or [])[:3])
    if tags:
        parts.append(tags)
    # The disclosure is appended LAST and protected from truncation: the body
    # is trimmed instead, so it can never be cut off.
    disclosure = AI_DISCLOSURE["de" if lang == "de" else "en"]
    text = "\n\n".join(parts)
    budget = 4800 - len(disclosure.encode("utf-8"))
    while len(text.encode("utf-8")) > budget:
        text = text[:-200]
    return text + "\n\n" + disclosure


def translate_lines(lines: list[str]) -> list[str]:
    """English caption lines -> German (same length). Raises on mismatch."""
    if not lines:
        return []
    raw = claude_cli.call_claude(TRANSLATE.format(lines=json.dumps(lines, ensure_ascii=False, indent=0)),
                                 component="translate_de", model=MODEL_STANDARD, allowed_tools=[], timeout_s=600)
    out = claude_cli.extract_json(raw)
    if not isinstance(out, list) or len(out) != len(lines):
        raise claude_cli.ClaudeError(f"translate_de: expected {len(lines)} lines, got {len(out) if isinstance(out, list) else '?'}")
    return [str(x) for x in out]
