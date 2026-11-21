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

# --- Local-only home (NOT on OneDrive) ----------------------------------- #
if platform.system() == "Windows":
    _APPDATA = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
else:
    _APPDATA = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
LOCAL_HOME = Path(os.getenv("LAUNCHPAD_LOCAL_HOME", str(_APPDATA / "launchpad")))

# .env loading: prefer the secure local-only path so the GitHub PAT never
# sits in a OneDrive-synced directory. Falls back to BASE_DIR for legacy.
_secure_env = LOCAL_HOME / ".env"
if _secure_env.exists():
    load_dotenv(_secure_env)
else:
    load_dotenv(BASE_DIR / ".env")

# --- GitHub --------------------------------------------------------------- #
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

# --- Models -------------------------------------------------------------- #
# Owner's call (2026-11-20): squeeze maximum quality out of the Max plan for
# launchpad. Creative and judgment roles (storyboard, hooks, titles,
# thumbnail concepts, critic) use the most capable model; routine roles use
# Opus. Every call falls down FALLBACK_MODELS on rate limits / overload, so
# the pipeline never stops ("orchestra must never stop").
MODEL_CREATIVE = os.getenv("LP_MODEL_CREATIVE", "claude-fable-5-2")
MODEL_STANDARD = os.getenv("LP_MODEL_STANDARD", "claude-opus-5-5")
EFFORT_CREATIVE = os.getenv("LP_EFFORT_CREATIVE", "high")
CLAUDE_MODEL = MODEL_STANDARD            # default when a caller picks no role
BUILDER_MODEL = CLAUDE_MODEL             # back-compat alias
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "LP_FALLBACK_MODELS", "claude-opus-5-5,claude-sonnet-5-5").split(",") if m.strip()]
FALLBACK_MODEL = FALLBACK_MODELS[-1]     # back-compat alias
# launchpad's PRIVATE Claude Code CLI. The shared global `claude` (2.1.68)
# is too old for current models but autodev/agent-radar depend on it, so
# launchpad keeps its own copy under LOCAL_HOME and auto-updates it.
PRIVATE_CLAUDE_DIR = LOCAL_HOME / "claude-cli"
PRIVATE_CLAUDE = PRIVATE_CLAUDE_DIR / "node_modules" / ".bin" / "claude.cmd"
CLAUDE_BIN = os.getenv("CLAUDE_BIN", str(PRIVATE_CLAUDE) if PRIVATE_CLAUDE.exists() else "claude")
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
# Engine order for tts.py's never-fail chain: the chosen backend first,
# then the others ("edge" -> "kokoro" -> "sapi").
TTS_BACKEND = os.getenv("TTS_BACKEND", "edge")
# Local fallback voice (Kokoro-82M) lives in an isolated venv so its deps
# never touch the shared Python install used by the other agents.
TTS_VENV_PY = LOCAL_HOME / "tts-venv" / "Scripts" / "python.exe"
KOKORO_MODEL_DIR = LOCAL_HOME / "models" / "kokoro"
TTS_LOCAL_VOICE = os.getenv("TTS_LOCAL_VOICE", "af_heart")
TTS_LOCAL_SPEED = float(os.getenv("TTS_LOCAL_SPEED", "1.0"))

# --- Pacing -------------------------------------------------------------- #
# Realistic uploads per week. YouTube's spam detection penalizes bursts.
MAX_UPLOADS_PER_WEEK = int(os.getenv("MAX_UPLOADS_PER_WEEK", "3"))
MIN_HOURS_BETWEEN_UPLOADS = int(os.getenv("MIN_HOURS_BETWEEN_UPLOADS", "12"))
WEEKDAY_UPLOAD_HOURS = (18, 23)
WEEKEND_UPLOAD_HOURS = (10, 22)

# --- Video config -------------------------------------------------------- #
VIDEO_TARGET_DURATION_SECONDS = int(os.getenv("VIDEO_TARGET_DURATION_SECONDS", "75"))
VIDEO_RESOLUTION = (1280, 720)   # 720p landscape for long-form
VIDEO_FPS = 24                    # we're recording slow scrolls; 24fps is plenty

# Vertical 9:16 for YouTube Shorts. 1080x1920 is YouTube's preferred
# Shorts resolution.
SHORTS_RESOLUTION = (1080, 1920)
SHORTS_TARGET_DURATION_SECONDS = int(os.getenv("SHORTS_TARGET_DURATION_SECONDS", "45"))
SHORTS_MAX_DURATION_SECONDS = 58  # YouTube cuts off Shorts at 60s

# --- YouTube Data API key (for stats monitoring — public read only) ------ #
# Different from OAuth: this is a simple API key for reading public video
# stats (views, likes, comments). Create at:
#   https://console.cloud.google.com/apis/credentials
#   -> Create Credentials -> API key
# YouTube Data API v3 must be enabled (already done for upload OAuth).
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

# --- Resilience ---------------------------------------------------------- #
MAX_VIDEO_RETRIES = int(os.getenv("MAX_VIDEO_RETRIES", "2"))
# A failed_terminal video is revived at the stage it died after this
# cooldown, up to MAX_REVIVES times, so a temporary outage (TTS, network,
# quota) can never permanently lose a video again.
REVIVE_AFTER_HOURS = int(os.getenv("REVIVE_AFTER_HOURS", "24"))
MAX_REVIVES = int(os.getenv("MAX_REVIVES", "3"))
# Alert if nothing has been uploaded for this long while work is pending.
STALE_UPLOAD_ALERT_DAYS = int(os.getenv("STALE_UPLOAD_ALERT_DAYS", "7"))
# Hugging Face Spaces auto-deploy: OFF. The owner does not want extra
# third-party services; set LP_HF_DEPLOY=1 to re-enable.
HF_DEPLOY_ENABLED = os.getenv("LP_HF_DEPLOY", "0") == "1"
# Motion-graphics engine (storyboard -> animated scenes). Set LP_MOTION=0 to
# fall back to the classic browser-scroll recorder for every video.
MOTION_ENGINE = os.getenv("LP_MOTION", "1") == "1"
# Extra Shorts (reach multiplier): more angles per project + fresh Shorts for
# the back catalogue, paced separately from long-form uploads.
EXTRA_SHORTS_ENABLED = os.getenv("LP_EXTRA_SHORTS", "1") == "1"
EXTRA_SHORTS_BACKCATALOG = os.getenv("LP_EXTRA_SHORTS_BACKCATALOG", "1") == "1"
EXTRA_SHORTS_PER_DAY = int(os.getenv("LP_EXTRA_SHORTS_PER_DAY", "1"))
