"""
Video compositor. Muxes the recorded .webm with the TTS audio into a
final .mp4 ready for YouTube upload. ffmpeg via imageio-ffmpeg (no
system install required).

Strategy:
  - Get audio duration.
  - Get video duration.
  - If audio longer than video: speed up audio slightly OR pad with
    silence padding. We pick speed adjustment within ±10% to stay
    natural.
  - Re-encode to H.264 + AAC (YouTube's preferred mp4).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _ffmpeg_path() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _media_duration(path: Path) -> float:
    """Use ffprobe-ish: parse `ffmpeg -i` stderr for Duration."""
    ff = _ffmpeg_path()
    proc = subprocess.run(
        [ff, "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    # ffmpeg writes "Duration: HH:MM:SS.ff" to stderr.
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            spec = line.split("Duration:", 1)[1].split(",", 1)[0].strip()
            h, m, s = spec.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise RuntimeError(f"could not parse duration of {path}")


def compose(*, video_in: Path, audio_in: Path, output_path: Path) -> Path:
    """
    Mux video+audio into output_path.mp4. Trims the longer track to match
    the shorter; if audio is longer than video, the video is held on its
    last frame for the extra seconds (so the voice-over finishes cleanly).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ff = _ffmpeg_path()

    v_dur = _media_duration(video_in)
    a_dur = _media_duration(audio_in)

    cmd = [ff, "-y"]

    # Audio normalization chain (EBU R128). Applied here so downstream
    # stages can stream-copy audio without ever pushing peaks past -1.5
    # dBTP. Values chosen for spoken narration:
    #   I=-16   integrated loudness target (YouTube-safe; Shorts plays
    #           loudly without auto-attenuation)
    #   TP=-1.5 true-peak ceiling (prevents the clipping we were seeing
    #           — max_volume sat at 0.0 dB with 2k+ clipped samples)
    #   LRA=11  loudness range (keeps natural dynamics; not over-compressed)
    # Followed by resample to 48k stereo so concat/polish stages don't
    # have to resample mid-pipeline (which is what corrupted the bitrate
    # on the previous build).
    # Note: imageio-ffmpeg's bundled binary lacks soxr — we use the default
    # swresample resampler, which is more than adequate for 24k -> 48k
    # speech (the artifact difference is well below the audible threshold).
    audio_chain = ("loudnorm=I=-16:TP=-1.5:LRA=11,"
                   "aresample=48000,"
                   "aformat=channel_layouts=stereo")

    if a_dur > v_dur + 0.5:
        # Hold the last video frame for (a_dur - v_dur) seconds via
        # tpad=stop_mode=clone. Audio plays in full.
        pad_s = a_dur - v_dur + 0.5
        cmd += [
            "-i", str(video_in),
            "-i", str(audio_in),
            "-filter_complex",
            (f"[0:v]tpad=stop_mode=clone:stop_duration={pad_s}[v];"
             f"[1:a]{audio_chain}[a]"),
            "-map", "[v]", "-map", "[a]",
        ]
    else:
        # Video is longer (or equal). Trim video to audio length so the
        # video doesn't dribble on after silence.
        cmd += [
            "-i", str(video_in),
            "-i", str(audio_in),
            "-filter_complex", f"[1:a]{audio_chain}[a]",
            "-map", "0:v", "-map", "[a]",
            "-t", str(a_dur),
        ]

    cmd += [
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-pix_fmt", "yuv420p",  # YouTube compatibility
        "-movflags", "+faststart",
        str(output_path),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg compose failed:\n{proc.stderr[-1500:]}")
    return output_path
