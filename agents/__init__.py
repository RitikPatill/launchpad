"""
Specialized agents for launchpad.

Roles:
  screenplay        — long-form voice-over script + recorder actions    [Sonnet]
  metadata          — long-form A/B title + description + tags          [Sonnet x2]
  shorts_screenplay — dedicated Shorts script (30-50s, hook-driven)     [Sonnet]
  shorts_metadata   — Shorts title + description + hashtags             [Sonnet]
  thumbnail         — PIL composite of key frame + title overlay        [no LLM]
  editor            — intro/outro cards + captions + Shorts polish      [no LLM]
  meta_improver     — weekly self-improvement, evidence-driven by stats [Opus 4.7]

Other deterministic Python modules: recorder (now supports vertical
viewport for Shorts), tts (SRT subtitles with fallback), compositor,
uploader, deployer (HF Spaces), monitor (YouTube stats poller).
"""

from . import (  # noqa: F401
    screenplay, metadata, shorts_screenplay, shorts_metadata,
    thumbnail, editor, meta_improver,
)
