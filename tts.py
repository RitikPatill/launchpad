"""
Text-to-speech via Microsoft Edge TTS. Free, no API key, decent quality.

We concatenate the per-scene voice-over lines with short pauses between
scenes so the audio track has natural breathing room.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from config import TTS_PITCH, TTS_RATE, TTS_VOICE


async def _synthesize_async(text: str, out_path: Path) -> None:
    import edge_tts  # type: ignore
    communicate = edge_tts.Communicate(
        text=text, voice=TTS_VOICE,
        rate=TTS_RATE, pitch=TTS_PITCH,
    )
    await communicate.save(str(out_path))


def synthesize_screenplay(screenplay: dict, out_path: Path) -> Path:
    """
    Concat all scene voice-over lines into one MP3. Insert SSML <break>
    tags between scenes for natural pacing — Edge TTS supports inline
    SSML break elements.
    """
    parts = []
    for scene in screenplay["scenes"]:
        voice = (scene.get("voice_over") or "").strip()
        if voice:
            parts.append(voice)
    # Edge TTS: SSML breaks aren't supported in plain mode; we use a real
    # period + space combination + newlines, which the model paces well.
    text = "\n\n".join(parts)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_synthesize_async(text, out_path))
    return out_path
