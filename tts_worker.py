"""
TTS worker. Runs inside the isolated venv at %LOCALAPPDATA%/launchpad/tts-venv
so its dependencies (edge-tts pinned, kokoro-onnx, onnxruntime) never touch
the shared Python install that other agents on this laptop rely on.

Called by tts.py as a subprocess. Two modes:

  edge   IN.txt OUT.mp3 VOICE RATE PITCH
         Microsoft Edge neural voice. Prints JSON:
         {"words": [{"offset_ms", "duration_ms", "text"}, ...]}

  kokoro IN.txt OUT.wav VOICE SPEED MODEL_DIR
         Local Kokoro-82M (offline). Synthesizes sentence by sentence and
         prints JSON: {"duration_s", "sentences": [{"text", "start_ms", "end_ms"}]}
         Sentence timings let tts.py build well-synced captions even though
         Kokoro does not emit word-boundary events.

The JSON result is always the LAST line of stdout.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

GAP_S = 0.18  # natural pause between sentences (kokoro mode)


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", text.strip())
    return [p.strip() for p in parts if p and p.strip()]


async def _edge(text: str, out_mp3: str, voice: str, rate: str, pitch: str) -> dict:
    import edge_tts
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch,
                                       boundary="WordBoundary")
    words, audio = [], bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            words.append({"offset_ms": int(chunk["offset"]) // 10000,
                          "duration_ms": int(chunk["duration"]) // 10000,
                          "text": chunk["text"]})
    Path(out_mp3).write_bytes(bytes(audio))
    return {"words": words}


def _kokoro(text: str, out_wav: str, voice: str, speed: str, model_dir: str) -> dict:
    import numpy as np
    import soundfile as sf
    from kokoro_onnx import Kokoro

    md = Path(model_dir)
    k = Kokoro(str(md / "kokoro-v1.0.onnx"), str(md / "voices-v1.0.bin"))
    chunks, timings, t, sr = [], [], 0.0, 24000
    for sent in split_sentences(text):
        samples, sr = k.create(sent, voice=voice, speed=float(speed), lang="en-us")
        dur = len(samples) / sr
        timings.append({"text": sent, "start_ms": int(t * 1000), "end_ms": int((t + dur) * 1000)})
        chunks.append(samples)
        chunks.append(np.zeros(int(GAP_S * sr), dtype=samples.dtype))
        t += dur + GAP_S
    if not chunks:
        raise SystemExit("empty text")
    audio = np.concatenate(chunks)
    sf.write(out_wav, audio, sr)
    return {"duration_s": len(audio) / sr, "sentences": timings}


def main() -> None:
    mode, text_file, out_path, *rest = sys.argv[1:]
    text = Path(text_file).read_text(encoding="utf-8")
    if mode == "edge":
        result = asyncio.run(_edge(text, out_path, *rest))
    elif mode == "kokoro":
        result = _kokoro(text, out_path, *rest)
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
