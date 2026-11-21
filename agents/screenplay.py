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
  scene 1 (10-15s) — THE HOOK. Open with a concrete claim or question
                     that gives the viewer a reason to stay past second
                     10.

                     SCENE-1 FORBIDDEN OPENINGS (HARD RULES, no
                     exceptions). Scene 1 voice_over MUST NOT begin with
                     any of these patterns:
                       - "This is <ProjectName> —" or "This is <X>, a …"
                       - "I built <X> …" or "I designed <X> …"
                       - "<ProjectName> is a …"
                       - "Today I'm walking through …"
                       - "Let me show you …"
                     These are introductions, not hooks. Empirically
                     (channel video_analytics 2026-06-06) the dry-intro
                     framing produced v12 long-form's 10.15% average
                     view percentage (viewers bounced inside the first
                     9s of an 89s video). The 2026-06-12 audit found v14
                     memkit regressed back to "This is MemKit — a
                     local-first semantic memory store I built…" despite
                     the prior edit, scoring 33.29% retention. The
                     contradiction was that the voice-over-style section
                     said "first-person ('I built')" — which is correct
                     for scenes 2 onward but NOT for scene 1. Scene 1 is
                     the hook; the first-person allowance starts at
                     scene 2.

                     By contrast v8 ("WebAct: Self-Correcting Browser
                     Agent in Python") and v11 ("Can an LLM Detect
                     Time-Series Anomalies from a CSV?") opened with a
                     concrete capability claim or question (no "this is"
                     / no "I built") and retained 56.7% / 51.7%
                     respectively. v16 rubricon and v17 agent-eval-lab
                     both correctly opened with "Most agent benchmarks
                     score the final answer only. This one scores the
                     trajectory…" — that is the target pattern.

                     SCENE-1 CATEGORICAL-GENERIC BAN (HARD RULE — added
                     2026-08-14 based on v30 contextweaver regression).
                     Scene 1 voice_over MUST NOT open with a categorical
                     third-person subject that talks ABOUT a class
                     instead of TO the viewer. Banned openers include:
                       - "Every <thing> …" ("Every LLM app hits the
                         token wall eventually…" — v30 pattern)
                       - "Most <things> …" / "All <things> …"
                       - "Developers who <verb> …"
                       - "There's a problem with <field> …"
                     These are essays, not hooks. Viewers scan for
                     something that names their situation, not a
                     category. v30 long-form used the "Every LLM app…"
                     opener and its Shorts sibling retained 9.60% —
                     channel-worst since the v16 rubricon regression
                     that the 2026-06-12 shorts edit was designed to
                     stop. Convert every categorical-generic hook into
                     a second-person or specific-first-instance framing
                     before writing scene 1. Preferred rewrites:
                       - "Your LLM app just hit the token wall. Now
                         what?" (second person + concrete pain)
                       - "Can an LLM app survive its own context
                         limit?" (specific question, no category)

                     SCENE-1 NOUN-DENSITY CAP (HARD RULE — added
                     2026-08-14 based on v32 mcp-eval-studio regression).
                     Scene 1 voice_over MUST contain AT MOST TWO core
                     technical noun-phrases (a "noun-phrase" here = an
                     acronym, a domain term like "MCP server" or
                     "eval harness", or a jargon compound). This cap is
                     separate from and stricter than the word cap.
                     Anti-example — v32 scene 1: "What if you could
                     point a Claude agent at any MCP server and get a
                     scored, traceable eval in seconds?" — 20 words
                     (inside the length cap) but FIVE stacked technical
                     nouns ("Claude agent", "MCP server", "eval",
                     "scored", "traceable"). Retention 25.18% long /
                     19.57% short. Compare with winners: v11 (2 nouns:
                     "LLM", "CSV" — 56.5% short ret), v27 (2 nouns:
                     "prompt", "yesterday/today" — 43.4% short ret),
                     v29 (2 nouns: "LLM", "question" — 39.4% short
                     ret). Every historical winner respected ≤2; every
                     recent loser stacked ≥3. If you find yourself
                     needing 3+ nouns to explain the hook, the hook is
                     wrong — the technical extension belongs in scene
                     2.

                     SCENE-1 LENGTH CAP (HARD RULE — added 2026-06-30
                     based on v21 incidentlens regression). Scene 1
                     voice_over MUST be ≤25 spoken words AND at most
                     two short sentences. Do NOT append a third
                     sentence. Do NOT chain a "here's how it works" or
                     "the way it does that" follow-up inside scene 1
                     — that technical extension belongs in scene 2.
                     v18 long-form (62.47% retention) opened with TWO
                     short sentences, ~14 words total. v21 long-form
                     (incidentlens 2026-06-29) opened with THREE
                     sentences and ~50 words; that is the same
                     compound + technical-extension shape that
                     produced shorts v16 rubricon's channel-worst
                     17.82% retention before the 2026-06-12 shorts
                     edit. Long-form viewers tolerate more density
                     than Shorts viewers, but density inside the hook
                     itself is still the swipe trigger. Land the
                     hook. Stop. Recorder moves to scene 2.

                     WORD COUNT AND SENTENCE COUNT ARE INDEPENDENT
                     RULES — BOTH MUST HOLD (added 2026-08-20 based
                     on v34 structextract regression; REINFORCED
                     2026-10-05 after v42 waypoint-agents regressed
                     the SAME failure mode six weeks later). THIS
                     RULE HAS NOW BEEN VIOLATED TWICE AFTER CODIFICATION
                     — HIGHEST-SALIENCE SCENE-1 RULE ON THIS SHEET.
                     Two anchors, both post-rule regressions:
                       - v34 structextract (2026-08-16): "You paste a
                         document into an LLM. You get prose back.
                         What if you got a typed, validated object
                         instead?" — 20 words (INSIDE 25-word cap)
                         but THREE sentences. Long-form retention
                         15.0 %, channel-worst since v29.
                       - v42 waypoint-agents (2026-09-27): "Kill an
                         agent mid-run. Restart it. Watch it pick up
                         exactly where it crashed." — 14 words (far
                         inside 25-word cap) but THREE imperative
                         sentences. Long-form 2 views / ~0 %
                         retention; Shorts sibling retained 29.2 %
                         (below channel median ~35 %, in the same
                         band as v16 rubricon's 17.82 % — the
                         compound-hook failure the length cap was
                         designed to prevent). Kinetic verbs did
                         NOT rescue it; the three-beat cadence
                         itself is the swipe trigger.
                     The failure mode is pattern-matching on word
                     count alone and ignoring the sentence-count
                     half of the rule. Count sentences explicitly
                     BEFORE finalising scene 1 — treat sentence
                     count as a separate pass over the draft, not
                     a side-effect of hitting word count. Even if
                     you are comfortably under 25 words, a third
                     sentence is forbidden. Both of these three-beat
                     shapes are banned:
                       - DECLARATIVE three-beat: "Do X. Get Y.
                         What if Z instead?" (v34 shape)
                       - IMPERATIVE three-beat: "Verb A. Verb B.
                         Watch/Then C." (v42 shape)
                     Both are instances of the v21 compound +
                     technical-extension shape; they collapse
                     retention regardless of total word count or
                     verb concreteness. If scene 1 reads as three
                     clipped sentences, FUSE the first two into one
                     ("Kill an agent mid-run and restart it — watch
                     it pick up where it crashed.") or drop the
                     weakest beat entirely. Two sentences max, no
                     exceptions.

                     SCENE-1 "WHAT-IF" DECAY GUARD (HARD RULE — added
                     2026-09-13 based on the v36→v37→v38 monotone
                     retention slide). The "What if you could/only…"
                     shape was validated by v36 codegraph-agent (42.0 %
                     long-form retention, best of the last 5) and has
                     been robotically re-applied since, each
                     re-application softer than the last:
                       - v36 "What if you could ask your codebase an
                          architectural question and *watch it reason
                          through* the answer?" — 42.0 % (kinetic
                          payoff verb).
                       - v37 "What if you *only paid for GPT-4* when
                          you actually needed it?" — 33.8 % (concrete
                          monetary payoff, still tangible).
                       - v38 "What if you could *grade your LLM's
                          output on a rubric* you wrote?" — 24.3 %,
                          channel-worst since v34 (procedural / abstract
                          payoff verbs stacked: grade, on-a-rubric,
                          wrote).
                     Rules that follow from this evidence:
                       (a) CONCRETE-VERB TEST. If scene 1 opens with
                           "What if…", the payoff verb inside the "What
                           if" clause MUST be sensory or kinetic (watch,
                           break, catch, fire, ship, crash, hit, land,
                           dump). It must NOT be abstract or
                           procedural (grade, evaluate, measure, define,
                           configure, compute, wire, orchestrate). Read
                           the hook aloud; if you cannot mime the verb
                           with one hand, it fails the test.
                       (b) SHAPE-VARIETY ROTATION. Do NOT default to
                           the "What if" shape by pattern-matching on
                           v36. When the concrete-verb test fails,
                           rotate to one of the other empirically-
                           validated shapes:
                             - CLAIM + COUNTER-CLAIM ("Most X do Y.
                               This one does Z.") — validated on v13
                               (channel-best Shorts 72.83 %), v17
                               agent-eval-lab, v16 rubricon long-form.
                             - CAN-YOU-QUESTION ("Can an LLM detect X
                               from a raw Y?") — validated on v11
                               (56.5 % Shorts, 51.7 % long).
                             - YOUR-SITUATION 2ND-PERSON (added
                               2026-10-19 based on the v45→v46→v47
                               ascending-retention streak). Open on
                               the viewer's concrete current state in
                               2nd person, then pivot with a question
                               or contrast. Two sub-shapes, both
                               validated:
                                 * YOUR-X-JUST-DID-Y + question —
                                   "Your GitHub backlog just hit 200
                                   open issues. Can an LLM actually
                                   triage that?" (v45, 33.73 % long)
                                   / "Your prompt worked yesterday.
                                   Did it survive the model update?"
                                   (v47, **66.95 %** long —
                                   channel-best of the last 15).
                                 * YOU-SEE-X-NOT-Y contrast —
                                   "You see the final answer. You
                                   don't see the 47 tool calls that
                                   got there." (v46, 43.63 % long).
                               Ascending retention over three
                               consecutive uploads
                               (33.73 % → 43.63 % → 66.95 %) with
                               the "What-if" / CAN-YOU shapes already
                               shipped multiple times in-between.
                               The signal is: 2nd-person situational
                               naming beats 2nd-person questioning
                               as an opener because it NAMES the
                               viewer's current state before asking
                               anything of them. Still obeys all
                               hard rules — v47 is 11 words / 2
                               sentences / 2 noun-phrases /
                               concrete payoff verb ("survive").
                           All three (plus WHAT-IF with kinetic
                           payoff) are rotation targets; the first
                           two were the patterns codified 2026-06-06
                           and remain unbeaten on retention; the
                           third is the fresh anchor as of 2026-10-19
                           (treat as soft preference, n=3).
                       (c) MONOTONY CHECK. If you have observed the
                           last three shipped scene-1 hooks all
                           opening with the same three-word prefix
                           ("What if you…"), that alone is enough
                           evidence to switch shapes for this video,
                           regardless of how well the current draft
                           passes rule (a).

                     Good scene-1 openers (mix of shapes; do not always
                     pick #2):
                       - "Most agent benchmarks score the final answer
                          only. This one scores the trajectory."
                          (CLAIM + COUNTER-CLAIM)
                       - "What if your agent could watch itself break
                          and fix the crash before you noticed?"
                          (WHAT-IF with kinetic payoff verbs —
                          watch, break, fix, crash)
                       - "Can you tell whether two documents were
                          written by the same person?"
                          (CAN-YOU-QUESTION)
                       - "Your prompt worked yesterday. Did it survive
                          the model update?"
                          (YOUR-SITUATION 2ND-PERSON — v47 anchor,
                          66.95 % long-form retention)
                     The voice-over should land the hook BEFORE the
                     recorder navigates anywhere — start with a 1-2s
                     wait on a static frame so the hook reads cleanly.
                     Recorder: goto repo URL, wait 1-2s on the README
                     header (the hook is delivered here), then small
                     scroll to reveal the rest.
  scene 2 (15-20s) — scroll through the README features/architecture
                     section. Voice explains the key technical choices.
  scene 3 (10-15s) — scroll to the Quickstart section, narrate how to
                     run it.
  scene 4 (15-25s, REQUIRED if PAGES URL is set, OPTIONAL otherwise) —
                     goto pages_url, narrate "and here's it running
                     live". This is the demo scene — viewers care about
                     seeing the actual product, not just code. Wait 4-6s
                     on the loaded page so viewers can see it. If the
                     page has interactive elements, scroll or wait for
                     them to render. SKIP this scene only if pages_url
                     is None or "(none)".
  final scene (5-10s) — back to repo, wrap up, mention author handle
                     (RitikPatill), close.

VOICE-OVER STYLE:
- Dry, technical. No marketing words: no "amazing", "powerful",
  "robust", "blazing". No emoji-equivalent vocal cues ("wow", "awesome").
- First-person ("I built", "I designed") is allowed FROM SCENE 2
  ONWARD. Scene 1 is the hook and follows the FORBIDDEN OPENINGS rules
  above — no "I built", no "This is <X>" in scene 1.
- Disclose AI involvement once: ONE sentence somewhere (not in scene 1)
  mentioning the project was built by an autonomous multi-agent system
  you designed.
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
