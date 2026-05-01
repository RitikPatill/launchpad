"""
Specialized agents for launchpad.

Roles:
  screenplay  — given a project, writes a 60-90s voice-over script
                AND a list of recorder actions timed to it           [Sonnet]
  metadata    — generates YouTube title, description, tags           [Sonnet]

All deterministic Python: recorder, tts, compositor, uploader.
"""

from . import screenplay, metadata  # noqa: F401
