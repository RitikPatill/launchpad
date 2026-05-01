"""
Central configuration for launchpad.

launchpad is the fourth orchestrator. Different mission again:
  autodev / agent-radar  —  CREATE GitHub projects
  oss-radar              —  CONTRIBUTE PRs (we dropped this)
  launchpad              —  AMPLIFY the work that ships, via YouTube videos

Watches autodev's and agent-radar's SQLite DBs (read-only) for projects
that have transitioned to status='done'. For each, generates a
walkthrough video (Playwright screen recording + AI voice-over),
uploads to YouTube with mandatory AI-content disclosure, embeds the
video into the project's README, and is done.

Cost discipline (carried over from oss-radar):
- Sonnet 4.6 ONLY for the LLM bits (screenplay + metadata).
- Free TTS via Microsoft Edge TTS (edge-tts package — zero cost,
  no API key, decent quality).
- ffmpeg via imageio-ffmpeg (Python package bundles the binary —
  no system install).
- YouTube Data API: free with daily quota.

Total recurring cost: $0/month.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# --- Local-only home (NOT on OneDrive) ----------------------------------- #
if platform.system() == "Windows":
    _APPDATA = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
else:
    _APPDATA = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
LOCAL_HOME = Path(os.getenv("LAUNCHPAD_LOCAL_HOME", str(_APPDATA / "launchpad")))

# --- GitHub --------------------------------------------------------------- #
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

# --- Models (Sonnet only) ------------------------------------------------- #
BUILDER_MODEL = os.getenv("BUILDER_MODEL", "claude-sonnet-4-6")
CLAUDE_MODEL = BUILDER_MODEL
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
CLAUDE_CALL_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_CALL_TIMEOUT_SECONDS", "1200"))

# --- Paths --------------------------------------------------------------- #
DATA_DIR = LOCAL_HOME / "data"
RENDER_DIR = LOCAL_HOME / "renders"        # raw recordings + audio + final mp4
LOG_DIR = BASE_DIR / "logs"
CREDS_DIR = LOCAL_HOME / "creds"           # YouTube OAuth tokens, client_secret.json
DB_PATH = DATA_DIR / "launchpad.db"
YT_CLIENT_SECRET_PATH = CREDS_DIR / "client_secret.json"
YT_TOKEN_PATH = CREDS_DIR / "youtube_token.json"

for d in (LOCAL_HOME, DATA_DIR, RENDER_DIR, LOG_DIR, CREDS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --- Sibling DB paths (we read these to discover projects to amplify) --- #
# autodev's and agent-radar's DBs live in their own LOCAL_HOMEs. Read-only
# SQLite from launchpad's perspective.
AUTODEV_DB = _APPDATA / "autodev" / "data" / "autodev.db"
AGENT_RADAR_DB = _APPDATA / "agent-radar" / "data" / "agent-radar.db"

# --- Throttles ----------------------------------------------------------- #
CLAUDE_CALLS_PER_5H = int(os.getenv("CLAUDE_CALLS_PER_5H", "10"))
ORCHESTRATOR_TICK_SECONDS = int(os.getenv("ORCHESTRATOR_TICK_SECONDS", "1800"))  # 30 min

# --- YouTube config ------------------------------------------------------ #
YT_PRIVACY_STATUS = os.getenv("YT_PRIVACY_STATUS", "public")  # public | unlisted | private
YT_CATEGORY_ID = os.getenv("YT_CATEGORY_ID", "28")            # 28 = Science & Technology
YT_DEFAULT_LANGUAGE = os.getenv("YT_DEFAULT_LANGUAGE", "en")

# AI-content disclosure flag (YouTube requires this for synthetic media).
# We always set this true because our videos use AI-generated voice.
YT_DECLARE_ALTERED = True

# --- TTS config ---------------------------------------------------------- #
# Microsoft Edge TTS voices, free for personal + commercial use.
# After 2026 community testing, Multilingual Neural voices sound notably
# more natural than the original Neural voices. Defaults below picked for
# minimum robotic feel:
#   en-US-EmmaMultilingualNeural — clear, warm, technical-content friendly
#   en-US-AndrewMultilingualNeural — male alternative
#   en-GB-RyanNeural — community pick for "least robotic"
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-EmmaMultilingualNeural")
TTS_RATE = os.getenv("TTS_RATE", "-5%")      # slightly slower than default - more natural
TTS_PITCH = os.getenv("TTS_PITCH", "+0Hz")
# Backend: "edge" (free, default) | "kokoro" (free local, more natural,
# requires `pip install kokoro` and ~100MB model download).
TTS_BACKEND = os.getenv("TTS_BACKEND", "edge")

# --- Pacing -------------------------------------------------------------- #
# Realistic uploads per week. YouTube's spam detection penalizes bursts.
MAX_UPLOADS_PER_WEEK = int(os.getenv("MAX_UPLOADS_PER_WEEK", "3"))
MIN_HOURS_BETWEEN_UPLOADS = int(os.getenv("MIN_HOURS_BETWEEN_UPLOADS", "12"))
WEEKDAY_UPLOAD_HOURS = (18, 23)
WEEKEND_UPLOAD_HOURS = (10, 22)

# --- Video config -------------------------------------------------------- #
VIDEO_TARGET_DURATION_SECONDS = int(os.getenv("VIDEO_TARGET_DURATION_SECONDS", "75"))
VIDEO_RESOLUTION = (1280, 720)   # 720p — keeps file size and render time low
VIDEO_FPS = 24                    # we're recording slow scrolls; 24fps is plenty

# --- Resilience ---------------------------------------------------------- #
MAX_VIDEO_RETRIES = int(os.getenv("MAX_VIDEO_RETRIES", "2"))
