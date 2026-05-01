"""
Specialized agents for launchpad.

Roles:
  screenplay  — given a project, writes a 60-90s voice-over script
                AND a list of recorder actions timed to it           [Sonnet]
  metadata    — A/B title generation + description + tags
                (with mandatory AI disclosure block)                 [Sonnet x2]
  thumbnail   — PIL composite of key frame + title overlay           [no LLM]

All other steps deterministic Python: recorder, tts (with SRT subtitles),
compositor, uploader (video + thumbnail + caption track).
"""

from . import screenplay, metadata, thumbnail  # noqa: F401
