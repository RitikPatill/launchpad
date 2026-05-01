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


def upload(*, video_path: Path, title: str, description: str,
           tags: list[str]) -> dict:
    """Upload one video. Returns {video_id, url}."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = _load_creds()
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags[:15],
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

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=4 * 1024 * 1024,
    )

    state.log("INFO", "uploader", f"uploading {video_path.name} ({title})")
    request = youtube.videos().insert(
        part="snippet,status",
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


def upload_thumbnail(*, video_id: str, thumbnail_path: Path) -> None:
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
    except Exception as e:
        # Common cause: channel not yet verified. Don't fail the whole
        # upload over a missing thumbnail; YouTube uses the first frame.
        state.log("WARN", "uploader",
                  f"thumbnail upload failed (channel may need phone verification): {e}")


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
