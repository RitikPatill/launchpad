"""
Storyboard scenes -> per-scene voice-over -> one padded narration track plus
a global timeline (scene start/end) and global word timings.

Each scene is voiced separately, so scene boundaries line up exactly with
the narration - no guessing where one visual should end.
"""
from __future__ import annotations

import re
import subprocess
import wave
from pathlib import Path

import tts

SR = 24000  # mono s16 PCM working format


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(round(max(0.0, seconds) * SR))


def _decode_pcm(src: Path) -> bytes:
    proc = subprocess.run(
        [tts._ffmpeg(), "-v", "error", "-i", str(src), "-f", "s16le", "-ac", "1",
         "-ar", str(SR), "-"], capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"decode failed for {src}: {proc.stderr[-300:]!r}")
    return proc.stdout


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _restore_punctuation(words: list[dict], text: str) -> list[dict]:
    """Edge word boundaries drop punctuation; captions need it (sentence
    breaks, commas). Re-align boundary words with the original tokens."""
    tokens = text.split()
    j = 0
    for w in words:
        target = _norm(w["text"])
        for k in range(j, min(j + 4, len(tokens))):
            if _norm(tokens[k]) == target and target:
                w["text"] = tokens[k]
                j = k + 1
                break
    return words


ENGINE_RANK = {"edge": 0, "kokoro": 1, "sapi": 2}


def build(scenes: list[dict], out_dir: Path) -> dict:
    """Voice every scene, assemble narration.wav, return the timeline.
    If the voice chain had to fall back for SOME scenes, every scene is
    re-voiced with that fallback engine so one video never switches narrator.

    Returns {"scenes": [{type, start, end, visual, voice_over}], "words":
    [{t0, t1, text}], "duration": float, "audio": Path, "engines": [..]}.
    """
    tl = _build(scenes, out_dir, only=None)
    for _ in range(2):                 # at most two re-voicing passes
        used = set(tl["engines"])
        if len(used) <= 1:
            break
        worst = max(used, key=lambda e: ENGINE_RANK.get(e, 9))
        tl = _build(scenes, out_dir, only=worst)
    return tl


def _build(scenes: list[dict], out_dir: Path, *, only: str | None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    pcm = bytearray()
    t = 0.0
    tl_scenes: list[dict] = []
    words: list[dict] = []
    engines: list[str] = []
    n = len(scenes)
    for i, sc in enumerate(scenes):
        text = (sc.get("voice_over") or "").strip()
        pad_in = 0.0 if i == 0 else 0.14
        pad_out = 1.1 if i == n - 1 else 0.26
        start = t
        pcm += _silence(pad_in)
        t += pad_in
        if text:
            mp3 = out_dir / f"vo_{i:02d}.mp3"
            ws, engine = tts.synthesize_text(text, mp3, only=only)
            engines.append(engine)
            audio = _decode_pcm(mp3)
            dur = len(audio) / 2 / SR
            if not ws:
                ws = tts._spans_to_words([{"text": text, "start_ms": 0, "end_ms": int(dur * 1000)}])
            ws = _restore_punctuation(ws, text)
            for w in ws:
                words.append({"t0": round(t + w["offset_ms"] / 1000, 3),
                              "t1": round(t + (w["offset_ms"] + w["duration_ms"]) / 1000, 3),
                              "text": w["text"]})
            pcm += audio
            t += dur
        else:
            hold = float(sc.get("hold_s") or 2.0)
            pcm += _silence(hold)
            t += hold
        pcm += _silence(pad_out)
        t += pad_out
        tl_scenes.append({"type": sc.get("type") or "statement", "start": round(start, 3),
                          "end": round(t, 3), "visual": dict(sc.get("visual") or {}),
                          "voice_over": text})
    if tl_scenes:
        tl_scenes[0]["visual"]["_instant"] = True
    wav = out_dir / "narration.wav"
    with wave.open(str(wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(bytes(pcm))
    return {"scenes": tl_scenes, "words": words, "duration": round(t, 3),
            "audio": wav, "engines": engines}


def to_srt(words: list[dict], words_per_cue: int = 7) -> str:
    ms = [{"offset_ms": int(w["t0"] * 1000), "duration_ms": int((w["t1"] - w["t0"]) * 1000),
           "text": w["text"]} for w in words]
    return tts._boundaries_to_srt(ms, words_per_cue=words_per_cue)


def chapters(tl_scenes: list[dict], labels: list[str] | None = None,
             duration: float | None = None) -> str:
    """YouTube chapter list ("0:00 Hook ...") from scene starts. YouTube needs
    the first chapter at 0:00, >= 3 chapters, and EVERY chapter >= 10 s -
    including the last one (otherwise the whole list is ignored)."""
    marks: list[tuple[float, str]] = []
    for i, s in enumerate(tl_scenes):
        label = (labels[i] if labels and i < len(labels) else "") or s["visual"].get("chapter") or ""
        if not label:
            continue
        if marks and s["start"] - marks[-1][0] < 10:
            continue
        marks.append((0.0 if not marks else s["start"], label))
    end = duration if duration is not None else (tl_scenes[-1]["end"] if tl_scenes else 0)
    while len(marks) > 1 and end - marks[-1][0] < 10:
        marks.pop()                     # fold a too-short final chapter into the previous one
    if len(marks) < 3:
        return ""
    return "\n".join(f"{int(a // 60)}:{int(a % 60):02d} {lbl}" for a, lbl in marks)
