"""
Specialized agents for launchpad.

Roles:
  screenplay     — voice-over script + recorder actions             [Sonnet]
  metadata       — A/B title + description + tags                   [Sonnet x2]
  thumbnail      — PIL composite of key frame + title overlay       [no LLM]
  editor         — intro/outro cards + burned-in captions           [no LLM]
  meta_improver  — weekly self-improvement of prompts above         [Opus 4.7]

All other steps deterministic Python: recorder, tts (with SRT subtitles),
compositor, uploader (video + thumbnail + caption track).
"""

from . import screenplay, metadata, thumbnail, editor, meta_improver  # noqa: F401
