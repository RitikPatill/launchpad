"""
Text-to-speech via Microsoft Edge TTS (free, no API key).

Generates BOTH the MP3 audio AND an SRT subtitle file with word-level
timing — Edge TTS exposes word-boundary events during synthesis. We
group ~7 words per subtitle cue to keep on-screen captions readable.

YouTube auto-captioning is fine for English speech but routinely
mangles technical terms (LLM, RAG, langgraph, etc.). Uploading our own
caption track gives us perfect transcription.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from config import TTS_PITCH, TTS_RATE, TTS_VOICE


def _format_srt_time(ms: int) -> str:
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"


async def _synthesize_async(text: str, audio_path: Path) -> list[dict]:
    """
    Stream synthesis from Edge TTS, capture audio + word boundary events.
    Returns list of {offset_ms, duration_ms, text} per word.
    """
    import edge_tts  # type: ignore

    communicate = edge_tts.Communicate(
        text=text, voice=TTS_VOICE,
        rate=TTS_RATE, pitch=TTS_PITCH,
    )
    boundaries: list[dict] = []
    audio_chunks = bytearray()

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            # offset in 100-nanosecond ticks; convert to ms
            offset_ms = int(chunk["offset"]) // 10000
            duration_ms = int(chunk["duration"]) // 10000
            boundaries.append({
                "offset_ms": offset_ms,
                "duration_ms": duration_ms,
                "text": chunk["text"],
            })

    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(bytes(audio_chunks))
    return boundaries


def _boundaries_to_srt(boundaries: list[dict],
                       words_per_cue: int = 7) -> str:
    """Group word-boundary events into readable SRT cues."""
    cues: list[str] = []
    if not boundaries:
        return ""
    for i in range(0, len(boundaries), words_per_cue):
        group = boundaries[i:i + words_per_cue]
        start = group[0]["offset_ms"]
        last = group[-1]
        end = last["offset_ms"] + last["duration_ms"]
        text = " ".join(w["text"] for w in group)
        idx = (i // words_per_cue) + 1
        cues.append(
            f"{idx}\n"
            f"{_format_srt_time(start)} --> {_format_srt_time(end)}\n"
            f"{text}\n"
        )
    return "\n".join(cues)


def _audio_duration_seconds(audio_path: Path) -> float:
    """Probe the MP3 we just wrote to get its duration. Used for the
    fallback SRT generator when Edge TTS doesn't emit word boundaries."""
    import subprocess
    import imageio_ffmpeg
    proc = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(audio_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            spec = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
            h, m, s = spec.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def _fallback_srt_from_text(text: str, total_seconds: float,
                            words_per_cue: int = 7) -> str:
    """
    When Edge TTS doesn't return word-boundary events, evenly time-split
    the script across the audio duration. Cruder timing but ensures we
    always ship some captions.
    """
    words = text.split()
    if not words or total_seconds <= 0:
        return ""
    per_word_ms = (total_seconds * 1000.0) / len(words)
    cues: list[str] = []
    for i in range(0, len(words), words_per_cue):
        group = words[i:i + words_per_cue]
        start_ms = int(i * per_word_ms)
        end_ms = int(min(len(words), i + words_per_cue) * per_word_ms)
        idx = (i // words_per_cue) + 1
        cues.append(
            f"{idx}\n"
            f"{_format_srt_time(start_ms)} --> {_format_srt_time(end_ms)}\n"
            f"{' '.join(group)}\n"
        )
    return "\n".join(cues)


def synthesize_screenplay(screenplay: dict, audio_path: Path,
                          srt_path: Path | None = None) -> tuple[Path, Path | None]:
    """
    Concat all scene voice-over lines, synthesize audio, write SRT.
    Falls back to even time-split if Edge TTS doesn't emit word boundaries.
    Returns (audio_path, srt_path_or_None).
    """
    parts: list[str] = []
    for scene in screenplay["scenes"]:
        voice = (scene.get("voice_over") or "").strip()
        if voice:
            parts.append(voice)
    text = "\n\n".join(parts)

    boundaries = asyncio.run(_synthesize_async(text, audio_path))
    if srt_path is None:
        return audio_path, None

    srt_path.parent.mkdir(parents=True, exist_ok=True)
    if boundaries:
        srt_path.write_text(_boundaries_to_srt(boundaries), encoding="utf-8")
    else:
        # Fallback: even time-split based on actual audio duration.
        duration = _audio_duration_seconds(audio_path)
        srt_text = _fallback_srt_from_text(text, duration)
        if not srt_text:
            return audio_path, None
        srt_path.write_text(srt_text, encoding="utf-8")
    return audio_path, srt_path
