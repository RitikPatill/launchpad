"""
Normalise and fact-check a storyboard before rendering.

Integrity guard: viewers include hiring managers, so nothing on screen or in
the narration may be invented. It FAILS CLOSED: whatever cannot be verified
is removed, never shown as fact.
- code     every line must exist in a COMMITTED source file or a README code
           block (not prose); the shown filename must exist; >= 2 substantive lines
- commands whole lines (continuations joined) must appear in README/docs;
           outputs likewise
- numbers  in stats, headlines, bullets, compare rows, node labels, terminal
           lines AND the narration (spelled-out numbers included) must appear
           in the material - with their unit ("10%", "3x") when they have one;
           stats additionally need a label word nearby
A missing workspace means nothing can be verified: no code, commands or stats.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from agents import repo_context

TYPES = {"hook", "statement", "bullets", "code", "terminal", "diagram", "stat", "stats",
         "compare", "browser", "cta"}
_WS = re.compile(r"\s+")
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(%|percent\b|x\b|×|times\b|ms\b|s\b|sec\b|"
                  r"seconds?\b|min\b|minutes?\b|h\b|hours?\b|k\b|K\b|M\b|B\b|GB\b|MB\b)?", re.I)
_UNIT_CANON = {"%": "%", "percent": "%", "x": "x", "×": "x", "times": "x"}

_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALE = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_NUMWORD = re.compile(r"\b(?:(?:%s|%s|%s|and|point)[\s-]*)+\b" % (
    "|".join(_ONES), "|".join(_TENS), "|".join(_SCALE)), re.I)


def _spoken_to_digits(text: str) -> str:
    """'forty-seven percent' -> '47 percent', 'three point five times' ->
    '3.5 times', so narration numbers go through the same check as digits."""
    def conv(m: re.Match) -> str:
        words = [w for w in re.split(r"[\s-]+", m.group(0).lower()) if w and w != "and"]
        if not words or all(w == "point" for w in words):
            return m.group(0)
        whole, cur, frac, in_frac = 0, 0, "", False
        for w in words:
            if w == "point":
                in_frac = True
            elif in_frac and w in _ONES and _ONES[w] < 10:
                frac += str(_ONES[w])
            elif w in _ONES:
                cur += _ONES[w]
            elif w in _TENS:
                cur += _TENS[w]
            elif w in _SCALE:
                cur = max(cur, 1) * _SCALE[w]
                if _SCALE[w] >= 1000:
                    whole, cur = whole + cur, 0
        n = whole + cur
        out = f"{n}.{frac}" if frac else str(n)
        trail = " " if m.group(0).endswith((" ", "-")) else ""
        return out + trail
    return _NUMWORD.sub(conv, text or "")


def _dec(s: str) -> str:
    try:
        d = Decimal(s.replace(",", "")).normalize()
        return format(d, "f")
    except (InvalidOperation, ValueError):
        return s


def _words(s: str) -> int:
    return len(str(s or "").split())


def _norm(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


def _readme_code(readme: str) -> str:
    return "\n".join(m.group(1) for m in re.finditer(r"```[^\n]*\n(.*?)```", readme or "", re.S))


def _cmd_corpus(readme: str) -> str:
    """README with shell line-continuations joined and '$ ' prompts stripped."""
    txt = re.sub(r"\\\s*\n\s*", " ", readme or "")
    txt = re.sub(r"(?m)^\s*[$>]\s+", "", txt)
    return _norm(txt)


def _num_index(raw: str) -> dict[str, set[str]]:
    """number (normalised) -> set of units it appears with ('' = bare)."""
    idx: dict[str, set[str]] = {}
    for m in _NUM.finditer(raw or ""):
        n = _dec(m.group(1))
        u = _UNIT_CANON.get((m.group(2) or "").lower(), "" if not m.group(2) else m.group(2).lower())
        idx.setdefault(n, set()).add(u)
    # Digits inside technical tokens (base64, utf8, gpt4, x86) also count as
    # present - as bare numbers only (units still have to match exactly).
    for m in re.finditer(r"(?<=[A-Za-z_])(\d+(?:\.\d+)?)", raw or ""):
        idx.setdefault(_dec(m.group(1)), set()).add("")
    return idx


def _near(raw: str, num: str, label: str, radius: int = 90) -> bool:
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", label or "")]
    if not words:
        return False
    low = raw.lower()
    pat = re.escape(num).replace(r"\.", r"\.?")
    for m in re.finditer(rf"(?<![\w.]){pat}(?![\d])", raw):
        window = low[max(0, m.start() - radius): m.end() + radius]
        if any(w in window for w in words):
            return True
    return False


def unsupported_numbers(text: str, idx: dict[str, set[str]]) -> list[str]:
    bad = []
    for m in _NUM.finditer(_spoken_to_digits(text)):
        n = _dec(m.group(1))
        unit_raw = (m.group(2) or "").lower()
        unit = _UNIT_CANON.get(unit_raw, unit_raw)
        if len(n.replace(".", "")) == 1 and not unit and "." not in n:
            continue                      # bare single digits are too common to check meaningfully
        units = idx.get(n)
        if units is None:
            bad.append(m.group(0).strip())
        elif unit in ("%", "x") and unit not in units:
            bad.append(m.group(0).strip())  # "10%" needs "10%" in the material, not just "10"
    return bad


def _repo_short(repo_url: str) -> str:
    return re.sub(r"^https?://", "", repo_url or "").rstrip("/")


def _visible_text(v: dict) -> list[str]:
    out = [str(v.get(k) or "") for k in ("headline", "sub", "title", "caption", "kicker")]
    out += [str(x) for x in (v.get("items") or []) if isinstance(x, str)]
    for it in v.get("items") or []:
        if isinstance(it, dict):
            out.append(f"{it.get('prefix', '')}{it.get('value_text', it.get('value', ''))}{it.get('suffix', '')} "
                       f"{it.get('label', '')}")
    for side in ("left", "right"):
        s = v.get(side) or {}
        if isinstance(s, dict):
            out.append(str(s.get("title") or ""))
            out += [str(x) for x in (s.get("items") or [])]
    for n in v.get("nodes") or []:
        if isinstance(n, dict):
            out += [str(n.get("label") or ""), str(n.get("sub") or "")]
    for ln in v.get("lines") or []:
        if isinstance(ln, dict):
            out.append(str(ln.get("cmd") or ln.get("out") or ""))
    return [t for t in out if t]


def normalise(sb: dict, *, kind: str, ctx: dict, repo_url: str) -> tuple[dict, list[str]]:
    """Returns (clean_storyboard, notes). Never raises on content problems.
    sb["unsupported_claims"] lists numbers that could not be verified."""
    notes: list[str] = []
    verifiable = bool(ctx.get("has_workspace"))
    readme = ctx.get("readme") or ""
    raw = readme + "\n" + "\n".join(f["text"] for f in ctx.get("files", []))
    idx = _num_index(raw)
    cmd_corpus = _cmd_corpus(readme)
    url = _repo_short(repo_url)
    scenes = []
    unsupported: list[str] = []
    for i, src in enumerate(sb.get("scenes") or []):
        if not isinstance(src, dict):
            continue
        t = str(src.get("type") or "statement").lower()
        if t not in TYPES:
            notes.append(f"scene {i}: unknown type {t} -> statement")
            t = "statement"
        v = dict(src.get("visual") or {})
        vo = str(src.get("voice_over") or "").strip()
        if t == "code":
            code = str(v.get("code") or "")
            lines = code.splitlines()[:16]
            v["code"] = "\n".join(lines)
            if not verifiable or not code.strip() or not repo_context.code_is_real(code, ctx, v.get("filename")):
                notes.append(f"scene {i}: code not verifiable in the published repo -> statement")
                t, v = "statement", {"headline": v.get("caption") or "Inside the code",
                                     "sub": v.get("filename", "")}
            else:
                n = len(lines)
                v["highlight"] = [int(x) for x in (v.get("highlight") or []) if str(x).isdigit() and 1 <= int(x) <= n]
        elif t == "terminal":
            kept = []
            for ln in (v.get("lines") or [])[:14]:
                if not isinstance(ln, dict):
                    continue
                if "cmd" in ln:
                    cmd = _norm(re.sub(r"^\s*[$>]\s+", "", str(ln["cmd"])))
                    if verifiable and cmd and cmd in cmd_corpus:
                        kept.append({"cmd": cmd[:200]})
                    else:
                        notes.append(f"scene {i}: dropped undocumented command")
                elif "out" in ln:
                    out = str(ln["out"])
                    parts = [p for p in (_norm(x) for x in out.split("...")) if p]
                    if verifiable and parts and all(p in _norm(readme) for p in parts):
                        kept.append({"out": out[:200]})
                    else:
                        notes.append(f"scene {i}: dropped undocumented terminal output")
            if any("cmd" in k for k in kept):
                v["lines"] = kept
            else:
                t, v = "statement", {"headline": v.get("title") or "Run it locally",
                                     "sub": "Setup steps are in the README"}
        elif t in ("stat", "stats"):
            items = []
            for it in (v.get("items") or [])[:3]:
                if not isinstance(it, dict):
                    continue
                vt = str(it.get("value_text") if it.get("value_text") is not None else it.get("value", "")).strip()
                vt = vt.replace(",", "")
                try:
                    float(vt)
                except ValueError:
                    continue
                n = _dec(vt)
                unit = _UNIT_CANON.get(str(it.get("suffix") or "").strip().lower(), "")
                units = idx.get(n)
                ok = (verifiable and units is not None and (not unit or unit in units)
                      and _near(raw, n, str(it.get("label", ""))))
                if not ok:
                    notes.append(f"scene {i}: stat {n}{unit} not supported by the material -> dropped")
                    continue
                items.append({**it, "value": float(n), "value_text": n})
            if not items:
                t, v = "statement", {"headline": v.get("title") or "What you get"}
            else:
                v["items"] = items
        elif t == "diagram":
            nodes = [n for n in (v.get("nodes") or []) if isinstance(n, dict)][:8]
            ids = {str(n.get("id", k)) for k, n in enumerate(nodes)}
            v["nodes"] = nodes
            v["edges"] = [e for e in (v.get("edges") or [])
                          if isinstance(e, (list, tuple)) and len(e) >= 2 and str(e[0]) in ids and str(e[1]) in ids]
            if len(nodes) < 2:
                t, v = "statement", {"headline": v.get("title") or "How it works"}
        elif t == "browser":
            v["image"] = "repo"
            v["url"] = url
        elif t == "cta":
            v["url"] = url
            v.setdefault("headline", "Code is on GitHub")
        elif t in ("hook", "statement"):
            v["headline"] = str(v.get("headline") or vo[:60])
        for txt in _visible_text(v) + [vo]:
            unsupported += unsupported_numbers(txt, idx)
        scene = {"type": t, "voice_over": vo, "visual": v}
        if src.get("chapter"):
            scene["chapter"] = str(src["chapter"])[:40]
            v["chapter"] = scene["chapter"]
        scenes.append(scene)

    if not scenes:
        raise ValueError("storyboard has no usable scenes")
    if scenes[0]["type"] != "hook":
        scenes[0]["type"] = "hook"
        scenes[0]["visual"].setdefault("headline", scenes[0]["voice_over"][:60])
        notes.append("first scene forced to hook")
    if kind == "long" and scenes[-1]["type"] != "cta":
        scenes.append({"type": "cta", "voice_over": "The code is on GitHub. Link in the description.",
                       "visual": {"headline": "Code is on GitHub", "url": url,
                                  "sub": "New AI build every week"}, "chapter": "Get the code"})
        notes.append("appended cta")
    total = sum(_words(s["voice_over"]) for s in scenes)
    budget = (200, 380) if kind == "long" else (55, 120)
    if not budget[0] <= total <= budget[1]:
        notes.append(f"word count {total} outside {budget}")
    out = dict(sb)
    out["scenes"] = scenes
    out["word_count"] = total
    out["unsupported_claims"] = sorted(set(unsupported))
    if out["unsupported_claims"]:
        notes.append(f"unsupported numbers: {out['unsupported_claims']}")
    return out, notes


def scrub(sb: dict, claims: list[str]) -> dict:
    """Fail-closed last resort: remove every sentence/item that still carries
    an unsupported number. Nothing unverified ships."""
    nums = {_dec(m.group(1)) for c in claims for m in _NUM.finditer(c)}
    if not nums:
        return sb

    def has_claim(text: str) -> bool:
        return any(_dec(m.group(1)) in nums for m in _NUM.finditer(_spoken_to_digits(text or "")))

    out = dict(sb)
    scenes = []
    for s in sb.get("scenes", []):
        s = dict(s)
        v = dict(s.get("visual") or {})
        sentences = re.split(r"(?<=[.!?])\s+", s.get("voice_over") or "")
        s["voice_over"] = " ".join(x for x in sentences if not has_claim(x)).strip()
        for k in ("sub", "caption", "kicker"):
            if has_claim(str(v.get(k) or "")):
                v.pop(k, None)
        for k in ("headline", "title"):
            if has_claim(str(v.get(k) or "")):
                v[k] = s.get("chapter") or ("Inside the project" if k == "headline" else "")
        if isinstance(v.get("items"), list):
            v["items"] = [x for x in v["items"] if not has_claim(x if isinstance(x, str) else str(x))]
        for side in ("left", "right"):
            if isinstance(v.get(side), dict):
                v[side] = {**v[side], "items": [x for x in v[side].get("items", []) if not has_claim(str(x))]}
        for n in v.get("nodes") or []:
            if isinstance(n, dict) and has_claim(str(n.get("sub") or "")):
                n.pop("sub", None)
        s["visual"] = v
        if s["voice_over"] or s.get("type") in ("hook", "cta"):
            scenes.append(s)
    out["scenes"] = scenes
    return out
