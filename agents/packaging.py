"""
Packaging agent: title, description, tags, thumbnail concept and German
localisation for a finished storyboard. Most capable model - titles and
thumbnails decide whether anyone clicks at all.
"""
from __future__ import annotations

import json
import re
import time

import claude_cli
from agents import channel_insights
from agents.storyboard import PLAYBOOK
from config import EFFORT_CREATIVE, LOCAL_HOME, MODEL_CREATIVE, MODEL_STANDARD

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

OTHER VIDEOS ON THIS CHANNEL (recent and queued). Your title and thumbnail text must take a clearly different angle and
wording from every one of these - a subscriber must never see two titles that sound alike:
{others}

Rules:
- Title <= 62 characters. Name the viewer's pain or the visual payoff, plus one searchable term (LLM, RAG, AI agent, eval, ...).
  No clickbait the video does not deliver, no ALL CAPS, no emoji, never the pattern "How to Build X with Y".
- description_intro: 2 sentences; the first 150 characters must work alone in search results.
- description_body: 3-6 short lines on what it does and how (plain text, may use "- " bullets). No repo link and no
  "built by" line: both are appended automatically.
- thumbnail.text: 2-4 punchy words that complement (not repeat) the title; thumbnail.emphasis: one of those words.
  thumbnail.visual_scene: the 0-based index of the scene whose visual best sells the video (prefer a diagram or code scene).
- tags: 10-15 specific search tags. hashtags: 3 (without spaces).
- de: natural German (not literal) title (<= 62 chars), description_intro and description_body for German viewers.
- pinned_comment: one genuine question that invites developers to reply.
- Every top-level field is English; German appears ONLY inside "de". Never put an unescaped double quote inside a JSON
  string (use single quotes or typographic quotes instead).

Return only JSON:
{{"title": "", "title_alternatives": ["", "", ""], "description_intro": "", "description_body": "", "tags": [],
  "hashtags": [], "thumbnail": {{"text": "", "emphasis": "", "visual_scene": 0}},
  "de": {{"title": "", "description_intro": "", "description_body": ""}}, "pinned_comment": ""}}"""

SHORT = """Package this YouTube Short for a developer channel (audience: developers and AI/ML hiring managers in Germany/EU).

PROJECT: {title} - {description}
SHORT SCRIPT: {script}

Rules: title <= 55 characters, written like the hook (tension or payoff), one searchable term, no clickbait the Short does
not deliver, no emoji, no ALL CAPS. description: 1-2 sentences. tags: 8-12. de: natural German title (<= 55 chars) and
description. title/description/tags are English; German appears ONLY inside "de". Never an unescaped double quote inside
a JSON string.
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
    return the dict that actually carries a title - never the German "de"
    block, which also has one."""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = obj[0]
    if isinstance(obj, dict) and "title" not in obj:
        for key, val in obj.items():
            if key not in ("de", "localizations") and isinstance(val, dict) and "title" in val:
                return val
    return obj


# German-only words. "im"/"mit" count only in lowercase: English writes IM / MIT.
_GERMAN_WORDS = re.compile(
    r"\b(der|das|dem|und|oder|nicht|kein|keine|dein|deine|deinen|deiner|deinem|du|dir|dich|für|wird|werden|wie|"
    r"warum|welche|welcher|ist|sind|ein|eine|einen|auf|zu|von|vom|zum|nur|auch|noch|jetzt|kann|zeigt|kaputt|"
    r"gemacht|echt|wenn|dann|schneller|besser|statt|heute|anders|immer|schon|hier|sich|dass|aber|doch|ohne|mehr|"
    r"neue|neuen|uns|unser|ihre|seine|alle|selbst|einfach|richtig|lokal|gleiche)\b|\b(?-i:im|mit)\b", re.IGNORECASE)
# (findall returns '' for the lowercase-only alternative; only the COUNT is used)
_ENGLISH_WORDS = re.compile(
    r"\b(the|and|your|you|with|for|is|are|of|to|on|it|its|this|that|what|why|how|when|from|can|does|not|but|"
    r"my|our|into|without|every|just)\b", re.IGNORECASE)
_UMLAUT = re.compile(r"[äöüßÄÖÜ]")
_TEXT_FIELDS = ("title", "description_intro", "description_body", "description")


def _looks_german(s, *, min_words: int = 2) -> bool:
    """German function words decide. One umlaut is only extra evidence (an
    English title may name Gödel, ETH Zürich or Schrödinger), and English
    function words outvote it."""
    s = str(s or "")
    de = len(_GERMAN_WORDS.findall(s))
    return de + bool(_UMLAUT.search(s)) >= min_words and de >= len(_ENGLISH_WORDS.findall(s))


def _as_text(pk) -> None:
    """Models sometimes return a text field as a list of lines: join it, so
    it can never crash build_description after a 30-minute render."""
    if not isinstance(pk, dict):
        return
    for d in (pk, pk.get("de") if isinstance(pk.get("de"), dict) else {}):
        for k in _TEXT_FIELDS:
            v = d.get(k)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                d[k] = "\n".join(x.strip() for x in v if x.strip())


def _common_problems(pk, *, min_tags: int, english: list[str]) -> list[str]:
    bad = []
    if not str(pk.get("title") or "").strip():
        bad.append("title missing")
    if not isinstance(pk.get("tags"), list) or len(pk["tags"]) < min_tags:
        bad.append(f"fewer than {min_tags} tags")
    de = pk.get("de")
    if not isinstance(de, dict) or not str(de.get("title") or "").strip():
        bad.append("de.title missing")
    for prefix, d in (("", pk), ("de.", de if isinstance(de, dict) else {})):
        bad += [f"{prefix}{k} must be a string" for k in _TEXT_FIELDS if d.get(k) and not isinstance(d[k], str)]
    if _looks_german(pk.get("title")):
        bad.append(f"title looks German ({str(pk.get('title'))[:70]!r}) - top-level fields must be English")
    bad += [f"{k} looks German - top-level fields must be English"
            for k in english if _looks_german(str(pk.get(k) or "")[:240], min_words=3)]
    return bad


def long_problems(pk) -> list[str]:
    """Why a packaging answer is unusable ([] = fine). Catches the failure
    that shipped German titles: a parse that fell back to the "de" block."""
    if not isinstance(pk, dict):
        return ["answer is not a JSON object"]
    bad = _common_problems(pk, min_tags=5, english=["description_intro"])
    if not str(pk.get("description_intro") or "").strip():
        bad.append("description_intro missing")
    th = pk.get("thumbnail")
    if not isinstance(th, dict) or not str(th.get("text") or "").strip():
        bad.append("thumbnail.text missing")
    return bad


def short_problems(pk) -> list[str]:
    if not isinstance(pk, dict):
        return ["answer is not a JSON object"]
    return _common_problems(pk, min_tags=3, english=["description"])


def retry_note(err: Exception) -> str:
    return (f"YOUR PREVIOUS ANSWER WAS REJECTED: {str(err)[:300]}. Return exactly one valid JSON object with every "
            "field filled: English everywhere except inside \"de\", and no unescaped double quotes inside strings.")


def _keep_raw(component: str, raw: str) -> None:
    """Keep rejected answers (local only, last 20) so a failure can be diagnosed."""
    try:
        d = LOCAL_HOME / "debug"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{component}-{int(time.time())}.txt").write_text(raw, encoding="utf-8")
        for old in sorted(d.glob(f"{component}-*.txt"))[:-20]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def _script_text(sb: dict) -> str:
    return "\n".join(f"[{i}] ({s['type']}) {s.get('voice_over', '')}" for i, s in enumerate(sb.get("scenes", [])))


def _others(titles: list[str] | None) -> str:
    return "\n".join(f"- {t}" for t in (titles or [])[:15]) or "- (none yet)"


def package_long(*, title: str, description: str, repo_url: str, storyboard: dict,
                 other_titles: list[str] | None = None, note: str = "") -> dict:
    prompt = LONG.format(title=title, description=description, repo_url=repo_url,
                         script=_script_text(storyboard), playbook=PLAYBOOK,
                         insights=channel_insights.summary(), others=_others(other_titles))
    raw = claude_cli.call_claude(prompt + ("\n\n" + note if note else ""),
                                 component="packaging_long", model=MODEL_CREATIVE, effort=EFFORT_CREATIVE,
                                 allowed_tools=[], timeout_s=900)
    pk = _unwrap(claude_cli.extract_json(raw))
    _as_text(pk)
    bad = long_problems(pk)
    if bad:
        _keep_raw("packaging_long", raw)
        raise claude_cli.ClaudeError("packaging_long rejected: " + "; ".join(bad))
    pk["title"] = str(pk["title"]).strip()[:95]
    pk.setdefault("hashtags", [])
    return pk


def package_short(*, title: str, description: str, storyboard: dict, note: str = "") -> dict:
    raw = claude_cli.call_claude(
        SHORT.format(title=title, description=description, script=_script_text(storyboard))
        + ("\n\n" + note if note else ""),
        component="packaging_short", model=MODEL_CREATIVE, effort=EFFORT_CREATIVE,
        allowed_tools=[], timeout_s=600)
    pk = _unwrap(claude_cli.extract_json(raw))
    _as_text(pk)
    bad = short_problems(pk)
    if bad:
        _keep_raw("packaging_short", raw)
        raise claude_cli.ClaudeError("packaging_short rejected: " + "; ".join(bad))
    pk["title"] = str(pk["title"]).strip()[:90]
    return pk


# "...multi-agent system I designed" / "...Multi-Agenten-System, das ich..." - the
# claim itself, not any mention of multi-agent (agent projects say that too).
_BUILT_BY = re.compile(r"multi-?agent\w*[- ]system\w*,?\s+(that\s+|das\s+)?(I|ich)\b|\bmy (own )?autonomous multi-?agent",
                       re.IGNORECASE)


def _mentions_url(text: str, url: str) -> bool:
    """True if `url` (scheme-less, case-insensitive, not as a prefix of a
    longer repo name) already appears in `text`."""
    u = re.sub(r"^https?://(www\.)?", "", (url or "").strip().lower()).rstrip("/")
    return bool(u) and re.search(re.escape(u) + r"(?![\w-])", (text or "").lower()) is not None


def build_description(pk: dict, *, chapters: str, repo_url: str, lang: str = "en",
                      long_form_url: str = "") -> str:
    """Assemble the final YouTube description (en or de)."""
    src = pk if lang == "en" else {**pk, **(pk.get("de") or {})}

    def clean(s) -> str:   # YouTube rejects < and > in descriptions
        if isinstance(s, list):
            s = "\n".join(str(x) for x in s)
        return str(s or "").replace("<", "‹").replace(">", "›").strip()

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
    if not _mentions_url(intro + "\n" + body, repo_url):     # the model may already have linked the repo
        parts.append(("Code (Open Source): " if lang == "de" else "Code (open source): ") + repo_url)
    if long_form_url:
        parts.append(("Ganzes Video: " if lang == "de" else "Full video: ") + long_form_url)
    if not _BUILT_BY.search(intro + "\n" + body):          # the model may already have made the claim
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
