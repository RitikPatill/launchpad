"""
YouTube uploader via YouTube Data API v3.

Auth model:
  - User runs `python orchestrator.py auth` ONCE. That:
      * loads creds/client_secret.json (downloaded from Google Cloud)
      * triggers a browser flow, user clicks "Allow"
      * saves creds/youtube_token.json (refresh token)
  - Subsequent uploads use the refresh token automatically — no further
    user interaction.

Disclosure: every uploaded video sets selfDeclaredMadeForKids=False AND
the new "altered_content" disclosure flag (declares synthetic audio).
"""
from __future__ import annotations

from pathlib import Path

import state
from config import (
    YT_CATEGORY_ID, YT_CLIENT_SECRET_PATH, YT_DECLARE_ALTERED,
    YT_DEFAULT_LANGUAGE, YT_PRIVACY_STATUS, YT_TOKEN_PATH,
)


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",  # captions + thumbnail
    "https://www.googleapis.com/auth/yt-analytics.readonly",  # retention/CTR
]


def authenticate_browser_flow() -> None:
    """One-time interactive flow. Saves a refresh token for future use."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not YT_CLIENT_SECRET_PATH.exists():
        raise RuntimeError(
            f"Place client_secret.json at {YT_CLIENT_SECRET_PATH} first.\n"
            "1. https://console.cloud.google.com/ -> create project\n"
            "2. APIs & Services -> Library -> enable YouTube Data API v3\n"
            "3. Credentials -> Create Credentials -> OAuth client ID\n"
            "   -> Application type: Desktop app -> download JSON\n"
            "4. Save as the path above"
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(YT_CLIENT_SECRET_PATH), SCOPES,
    )
    creds = flow.run_local_server(port=0)
    YT_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"saved YouTube credentials to {YT_TOKEN_PATH}")


def _load_creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not YT_TOKEN_PATH.exists():
        raise RuntimeError(
            f"YouTube token not found at {YT_TOKEN_PATH}.\n"
            "Run: python orchestrator.py auth"
        )
    creds = Credentials.from_authorized_user_file(str(YT_TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        YT_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _clean_text(s: str, *, max_chars: int | None = None, max_bytes: int | None = None) -> str:
    """YouTube rejects '<' and '>' in titles/descriptions (invalidTitle /
    invalidDescription) - LLM-written text can contain them (e.g. '->')."""
    s = (s or "").replace("<", "‹").replace(">", "›").strip()
    if max_chars is not None:
        s = s[:max_chars]
    if max_bytes is not None:
        while len(s.encode("utf-8")) > max_bytes:
            s = s[:-50]
    return s


def _clean_tags(tags: list[str], budget: int = 480) -> list[str]:
    """YouTube's tag budget is 500 characters, where a tag containing a space
    counts with its surrounding quotes, plus separators."""
    out, used = [], 0
    for t in tags or []:
        t = _clean_text(str(t), max_chars=100).replace(",", " ")
        if not t:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if out else 0)
        if used + cost > budget:
            break
        out.append(t)
        used += cost
    return out[:15]


def upload(*, video_path: Path, title: str, description: str,
           tags: list[str], localizations: dict | None = None) -> dict:
    """Upload one video. Returns {video_id, url}. `localizations` e.g.
    {"de": {"title": ..., "description": ...}} shows German metadata to
    German-language viewers."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _load_creds()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    title = _clean_text(title, max_chars=100) or "Open-source AI project"
    description = _clean_text(description, max_bytes=4990)
    tags = _clean_tags(tags)
    if localizations:
        localizations = {
            lang: {"title": _clean_text(loc.get("title", ""), max_chars=100) or title,
                   "description": _clean_text(loc.get("description", ""), max_bytes=4990)}
            for lang, loc in localizations.items() if isinstance(loc, dict)}
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": YT_CATEGORY_ID,
            "defaultLanguage": YT_DEFAULT_LANGUAGE,
            "defaultAudioLanguage": YT_DEFAULT_LANGUAGE,
        },
        "status": {
            "privacyStatus": YT_PRIVACY_STATUS,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": YT_DECLARE_ALTERED,  # AI disclosure
        },
    }
    parts = "snippet,status"
    if localizations:
        body["localizations"] = localizations
        parts += ",localizations"

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=4 * 1024 * 1024,
    )

    state.log("INFO", "uploader", f"uploading {video_path.name} ({title})")
    request = youtube.videos().insert(
        part=parts,
        body=body,
        media_body=media,
    )
    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
        except Exception as e:
            raise RuntimeError(f"upload failed: {e}")
    video_id = response["id"]
    url = f"https://youtu.be/{video_id}"
    state.log("INFO", "uploader", f"uploaded: {url}")
    return {"video_id": video_id, "url": url}


def upload_thumbnail(*, video_id: str, thumbnail_path: Path) -> bool:
    """Set a custom thumbnail. Requires the channel to be verified —
    YouTube enables this once you've verified via phone."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _load_creds()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    media = MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg")
    try:
        youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
        state.log("INFO", "uploader", f"thumbnail set on {video_id}")
        return True
    except Exception as e:
        # Common cause: channel not yet verified. Don't fail the whole
        # upload over a missing thumbnail; YouTube uses the first frame.
        why = ("rate-limited by YouTube, will retry later" if "uploadRateLimitExceeded" in str(e)
               else "channel may need phone verification" if "permissions" in str(e) else "error")
        state.log("WARN", "uploader", f"thumbnail upload failed ({why}): {str(e)[:300]}")
        return False


def set_video_privacy(*, video_id: str, privacy: str) -> None:
    """Flip a video's privacyStatus. Used to retire a broken Short after
    a fixed re-upload — YouTube has no in-place replace, so we publish
    the corrected video as new and private the old one (keeps it
    recoverable for diffing, hides it from viewers/search)."""
    assert privacy in ("public", "unlisted", "private"), privacy
    from googleapiclient.discovery import build

    creds = _load_creds()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    youtube.videos().update(
        part="status",
        body={"id": video_id, "status": {"privacyStatus": privacy}},
    ).execute()
    state.log("INFO", "uploader", f"set {video_id} privacyStatus={privacy}")


def upload_caption(*, video_id: str, srt_path: Path,
                   language: str = "en", name: str = "English") -> None:
    """Attach an SRT caption track to the uploaded video."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _load_creds()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "videoId": video_id,
            "language": language,
            "name": name,
            "isDraft": False,
        }
    }
    media = MediaFileUpload(str(srt_path), mimetype="application/octet-stream")
    try:
        youtube.captions().insert(part="snippet", body=body, media_body=media).execute()
        state.log("INFO", "uploader", f"caption uploaded for {video_id}")
    except Exception as e:
        state.log("WARN", "uploader", f"caption upload failed: {e}")
