"""
Text-to-speech with a never-fail engine chain.

  1. Microsoft Edge TTS (edge-tts)    - natural neural voice, word timings
  2. Kokoro-82M, local on this laptop - neural, offline, sentence timings
  3. Windows SAPI (System.Speech)     - robotic, but always available

launchpad died for 39 days (Oct-Nov 2026) because edge-tts was the ONLY
engine and Microsoft started rejecting old client versions with HTTP 403.
Now a failure of one engine falls through to the next and raises an
alert, so a video is never lost to a voice outage again.

Also writes an SRT caption file. Edge provides word-level timings, but
only when asked for boundary="WordBoundary": since edge-tts 7 the default
is sentence boundaries, which silently broke our caption sync. Kokoro
provides sentence timings, which we spread over words by character
length. SAPI gets a duration-proportional split.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path

from config import (
    KOKORO_MODEL_DIR, TTS_BACKEND, TTS_LOCAL_SPEED, TTS_LOCAL_VOICE,
    TTS_PITCH, TTS_RATE, TTS_VENV_PY, TTS_VOICE,
)

_WORKER = Path(__file__).resolve().parent / "tts_worker.py"
LAST_ENGINE: str | None = None   # which engine produced the last audio


# --------------------------------------------------------------------- #
# SRT helpers
# --------------------------------------------------------------------- #

def _format_srt_time(ms: int) -> str:
    h, rem = divmod(max(0, int(ms)), 3600000)
    m, rem = divmod(rem, 60000)
    s, ms2 = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms2:03d}"


def _boundaries_to_srt(boundaries: list[dict], words_per_cue: int = 7) -> str:
    """Group word timings ({offset_ms, duration_ms, text}) into SRT cues."""
    cues: list[str] = []
    for i in range(0, len(boundaries), words_per_cue):
        group = boundaries[i:i + words_per_cue]
        start = group[0]["offset_ms"]
        end = group[-1]["offset_ms"] + group[-1]["duration_ms"]
        text = " ".join(w["text"] for w in group)
        cues.append(f"{i // words_per_cue + 1}\n"
                    f"{_format_srt_time(start)} --> {_format_srt_time(end)}\n"
                    f"{text}\n")
    return "\n".join(cues)


def _spans_to_words(spans: list[dict]) -> list[dict]:
    """Spread each timed span ({text, start_ms, end_ms}) over its words,
    weighting by character length (long words take longer to say)."""
    words_out: list[dict] = []
    for sp in spans:
        words = sp["text"].split()
        if not words:
            continue
        total = sum(len(w) + 1 for w in words)
        span_ms = max(1, sp["end_ms"] - sp["start_ms"])
        t = float(sp["start_ms"])
        for w in words:
            d = span_ms * (len(w) + 1) / total
            words_out.append({"offset_ms": int(t), "duration_ms": int(d), "text": w})
            t += d
    return words_out


# --------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------- #

def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _audio_duration_seconds(audio_path: Path) -> float:
    proc = subprocess.run([_ffmpeg(), "-i", str(audio_path)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            spec = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
            h, m, s = spec.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def _wav_to_mp3(wav: Path, mp3: Path) -> None:
    mp3.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(wav),
         "-codec:a", "libmp3lame", "-b:a", "160k", str(mp3)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not mp3.exists():
        raise RuntimeError(f"wav->mp3 failed: {proc.stderr[-300:]}")


def _check_audio(path: Path) -> None:
    if not path.exists() or path.stat().st_size < 2000:
        raise RuntimeError(f"engine produced no usable audio ({path})")


# --------------------------------------------------------------------- #
# Engines. Each writes audio_path (mp3) and returns word timings
# (possibly empty).
# --------------------------------------------------------------------- #

async def _edge_async(text: str, audio_path: Path) -> list[dict]:
    import edge_tts  # type: ignore
    communicate = edge_tts.Communicate(
        text=text, voice=TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH,
        boundary="WordBoundary",
    )
    boundaries: list[dict] = []
    audio = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            boundaries.append({
                "offset_ms": int(chunk["offset"]) // 10000,
                "duration_ms": int(chunk["duration"]) // 10000,
                "text": chunk["text"],
            })
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(bytes(audio))
    return boundaries


def _edge_via_worker(text: str, audio_path: Path) -> list[dict]:
    """edge-tts from the isolated tts-venv (pinned, current version), so a
    stale shared install can never break us again."""
    with tempfile.TemporaryDirectory() as td:
        txt = Path(td) / "in.txt"
        txt.write_text(text, encoding="utf-8")
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [str(TTS_VENV_PY), str(_WORKER), "edge", str(txt), str(audio_path),
             TTS_VOICE, TTS_RATE, TTS_PITCH],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600,
        )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout)[-400:])
    return json.loads(proc.stdout.strip().splitlines()[-1]).get("words", [])


def _engine_edge(text: str, audio_path: Path) -> list[dict]:
    use_worker = TTS_VENV_PY.exists()
    last: Exception | None = None
    for attempt in range(3):
        try:
            if use_worker:
                words = _edge_via_worker(text, audio_path)
            else:
                words = asyncio.run(_edge_async(text, audio_path))
            _check_audio(audio_path)
            return words
        except Exception as e:  # 403 / timeout / empty audio
            last = e
            if "403" in str(e) and attempt >= 1:
                break           # a hard block will not clear up in seconds
            time.sleep(5 * (attempt + 1) ** 2)
    raise RuntimeError(f"edge-tts failed: {last}")


def _engine_kokoro(text: str, audio_path: Path) -> list[dict]:
    if not TTS_VENV_PY.exists() or not (KOKORO_MODEL_DIR / "kokoro-v1.0.onnx").exists():
        raise RuntimeError("local Kokoro voice not installed")
    with tempfile.TemporaryDirectory() as td:
        txt, wav = Path(td) / "in.txt", Path(td) / "out.wav"
        txt.write_text(text, encoding="utf-8")
        proc = subprocess.run(
            [str(TTS_VENV_PY), str(_WORKER), "kokoro", str(txt), str(wav),
             TTS_LOCAL_VOICE, str(TTS_LOCAL_SPEED), str(KOKORO_MODEL_DIR)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1800,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"kokoro worker failed: {proc.stderr[-400:]}")
        meta = json.loads(proc.stdout.strip().splitlines()[-1])
        _wav_to_mp3(wav, audio_path)
    _check_audio(audio_path)
    return _spans_to_words(meta.get("sentences", []))


def _engine_sapi(text: str, audio_path: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        txt, wav = Path(td) / "in.txt", Path(td) / "out.wav"
        txt.write_text(text, encoding="utf-8")
        ps = ("Add-Type -AssemblyName System.Speech;"
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              f"$s.SetOutputToWaveFile('{wav}');"
              f"$s.Speak([IO.File]::ReadAllText('{txt}'));"
              "$s.Dispose()")
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                              capture_output=True, text=True, timeout=900)
        if proc.returncode != 0 or not wav.exists():
            raise RuntimeError(f"SAPI failed: {proc.stderr[-300:]}")
        _wav_to_mp3(wav, audio_path)
    _check_audio(audio_path)
    return []  # no timings; caller spreads words over the duration


_ENGINES = {"edge": _engine_edge, "kokoro": _engine_kokoro, "sapi": _engine_sapi}


def _engine_order() -> list[str]:
    first = TTS_BACKEND if TTS_BACKEND in _ENGINES else "edge"
    return [first] + [e for e in ("edge", "kokoro", "sapi") if e != first]


# --------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------- #

def synthesize_text(text: str, audio_path: Path, *, only: str | None = None) -> tuple[list[dict], str]:
    """Run the engine chain. Returns (word_timings, engine_name).
    `only` starts the chain at that engine (used to keep one narrator per
    video). Raises only if every engine failed."""
    global LAST_ENGINE
    errors: list[str] = []
    order = _engine_order()
    if only in _ENGINES:
        order = order[order.index(only):] if only in order else [only]
    for name in order:
        try:
            words = _ENGINES[name](text, audio_path)
        except Exception as e:
            errors.append(f"{name}: {str(e)[:300]}")
            continue
        LAST_ENGINE = name
        if errors:
            try:
                import alerts
                alerts.notify(f"tts-degraded-{name}", "Voice engine fell back",
                              f"used '{name}' because: {' | '.join(errors)}")
            except Exception:
                pass
        return words, name
    raise RuntimeError("all TTS engines failed -> " + " || ".join(errors))


def synthesize_screenplay(screenplay: dict, audio_path: Path,
                          srt_path: Path | None = None,
                          *, shorts_mode: bool = False) -> tuple[Path, Path | None]:
    """
    Concat all scene voice-over lines, synthesize audio, write SRT.
    `shorts_mode=True` gives snappy 3-word cues; long-form uses 7-word cues.
    Returns (audio_path, srt_path_or_None).
    """
    parts = [(s.get("voice_over") or "").strip() for s in screenplay["scenes"]]
    text = "\n\n".join(p for p in parts if p)
    words_per_cue = 3 if shorts_mode else 7

    words, _engine = synthesize_text(text, audio_path)
    if srt_path is None:
        return audio_path, None
    if not words:
        dur = _audio_duration_seconds(audio_path)
        if dur:
            words = _spans_to_words([{"text": text, "start_ms": 0, "end_ms": int(dur * 1000)}])
    if not words:
        return audio_path, None
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(_boundaries_to_srt(words, words_per_cue=words_per_cue), encoding="utf-8")
    return audio_path, srt_path
