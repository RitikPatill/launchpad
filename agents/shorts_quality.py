"""
Quality-gate agent for YouTube Shorts.

Probes a rendered final .mp4 (and its sidecar SRT, when present) and
returns a structured report covering the failure modes that have
actually bitten this pipeline in production:

  - audio clipping (max_volume >= -0.5 dB or non-zero histogram_0db)
  - audio loudness off-target (mean dB outside the spoken-voice band)
  - audio sample-rate degraded (anything below 44.1 kHz means a
    downstream resample is going to add artifacts)
  - resolution not 1080x1920 (Shorts shelf will not pick it up)
  - duration outside the 5-60s window (cut off / not eligible)
  - filesize over YouTube's per-resumable-chunk sweet spot
  - caption density too high (>4 words/cue reads as a "wall of text"
    on a 1080x1920 canvas held in one hand)

Free — no LLM calls, no external services. Pure ffmpeg + regex.

Designed to run inline at the end of `_step_shorts_rendering` in the
orchestrator. The current pipeline treats Shorts as a bonus, so this
agent logs failures via `state.log` rather than aborting the upload —
but the metrics it captures are exactly the signal the weekly Opus
meta-improver needs to drive further upstream fixes.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


@dataclass
class Issue:
    severity: str  # "critical" | "warning" | "info"
    code: str
    message: str
    fix_hint: str = ""


@dataclass
class QualityReport:
    passed: bool
    metrics: dict
    issues: list[Issue] = field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def summary(self) -> str:
        if self.passed:
            return f"PASS (warnings: {self.warning_count})"
        return f"FAIL — critical: {self.critical_count}, warnings: {self.warning_count}"


# --- Probes ------------------------------------------------------------ #

def _probe_volume(video_path: Path) -> dict:
    """volumedetect parser: peak, mean, # clipped samples."""
    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostats", "-i", str(video_path),
         "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out: dict = {}
    for line in proc.stderr.splitlines():
        m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", line)
        if m:
            out["mean_db"] = float(m.group(1))
        m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", line)
        if m:
            out["max_db"] = float(m.group(1))
        m = re.search(r"histogram_0db:\s*(\d+)", line)
        if m:
            out["clipped_samples"] = int(m.group(1))
    return out


def _probe_stream_info(video_path: Path) -> dict:
    """Single ffmpeg -i call, parse stderr for resolution/fps/audio/duration."""
    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(video_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    info: dict = {}
    for line in proc.stderr.splitlines():
        m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", line)
        if m:
            h, mm, ss = m.groups()
            info["duration_s"] = int(h) * 3600 + int(mm) * 60 + float(ss)
        m = re.search(r"Video:.*?(\d{3,4})x(\d{3,4})", line)
        if m:
            info["width"] = int(m.group(1))
            info["height"] = int(m.group(2))
        m = re.search(r"Video:.*?,\s*([\d.]+)\s*fps", line)
        if m:
            info["fps"] = float(m.group(1))
        m = re.search(r"Audio:.*?(\d+)\s*Hz,\s*(mono|stereo|\d+\.\d+|\d+ channels)", line)
        if m:
            info["audio_hz"] = int(m.group(1))
            info["audio_layout"] = m.group(2)
        m = re.search(r"Audio:.*?(\d+)\s*kb/s", line)
        if m:
            info["audio_kbps"] = int(m.group(1))
    if video_path.exists():
        info["filesize_mb"] = round(video_path.stat().st_size / (1024 * 1024), 2)
    return info


def _probe_caption_density(srt_path: Path | None) -> dict:
    """Max / avg words per cue. Netflix-style for vertical Shorts caps
    at 3-4 words per cue."""
    if not srt_path or not srt_path.exists():
        return {"max_words_per_cue": 0, "avg_words_per_cue": 0.0, "cue_count": 0}

    text = srt_path.read_text(encoding="utf-8", errors="replace")
    word_counts: list[int] = []
    cur_text: list[str] = []
    in_cue = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if cur_text:
                word_counts.append(sum(len(c.split()) for c in cur_text))
                cur_text = []
            in_cue = False
            continue
        if "-->" in line:
            in_cue = True
            continue
        if in_cue:
            cur_text.append(line)
    if cur_text:
        word_counts.append(sum(len(c.split()) for c in cur_text))

    if not word_counts:
        return {"max_words_per_cue": 0, "avg_words_per_cue": 0.0, "cue_count": 0}
    return {
        "max_words_per_cue": max(word_counts),
        "avg_words_per_cue": round(sum(word_counts) / len(word_counts), 2),
        "cue_count": len(word_counts),
    }


# --- Top-level assessment --------------------------------------------- #

def assess(*, video_path: Path, srt_path: Path | None = None) -> QualityReport:
    """Run all probes and return a structured report."""
    info = _probe_stream_info(video_path)
    vol = _probe_volume(video_path)
    captions = _probe_caption_density(srt_path)
    metrics: dict = {**info, **vol, **captions}
    issues: list[Issue] = []

    # --- audio ---
    max_db = vol.get("max_db")
    mean_db = vol.get("mean_db")
    clipped = vol.get("clipped_samples", 0)

    if max_db is not None and (max_db >= -0.5 or clipped > 0):
        issues.append(Issue(
            "critical", "audio_clipping",
            f"max_volume={max_db:.1f} dB, {clipped} samples at 0 dB",
            "compose must apply loudnorm I=-16 TP=-1.5 BEFORE the polish stage",
        ))
    if mean_db is not None and mean_db < -23:
        issues.append(Issue(
            "warning", "audio_too_quiet",
            f"mean_volume={mean_db:.1f} dB (target band: -20 .. -14)",
            "verify loudnorm I=-16 actually ran (check ffmpeg stderr)",
        ))
    if mean_db is not None and mean_db > -10:
        issues.append(Issue(
            "warning", "audio_too_loud",
            f"mean_volume={mean_db:.1f} dB (target band: -20 .. -14)",
            "lower loudnorm I target (e.g. I=-18)",
        ))

    audio_hz = info.get("audio_hz", 0)
    if audio_hz and audio_hz < 44100:
        issues.append(Issue(
            "warning", "audio_low_sample_rate",
            f"sample rate {audio_hz} Hz < 44100 (YouTube upsampling causes artifacts)",
            "ensure compose uses -ar 48000",
        ))

    audio_kbps = info.get("audio_kbps", 0)
    if audio_kbps and audio_kbps < 96:
        issues.append(Issue(
            "warning", "audio_low_bitrate",
            f"audio bitrate {audio_kbps} kb/s — concat may have downcoded; expected ~192",
            "verify polish concat uses -b:a 192k -ac 2 -ar 48000",
        ))

    # --- video ---
    w, h = info.get("width", 0), info.get("height", 0)
    if (w, h) != (1080, 1920):
        issues.append(Issue(
            "critical", "wrong_resolution",
            f"resolution {w}x{h} (Shorts shelf requires exactly 1080x1920)",
            "check SHORTS_RESOLUTION in config + recorder viewport",
        ))

    dur = info.get("duration_s", 0)
    if dur and dur < 5:
        issues.append(Issue(
            "critical", "too_short",
            f"duration {dur:.1f}s (<5s ineligible for Shorts shelf)",
        ))
    elif dur and dur > 60:
        issues.append(Issue(
            "critical", "too_long",
            f"duration {dur:.1f}s (>60s drops out of Shorts shelf)",
            "check SHORTS_MAX_DURATION_SECONDS + screenplay length cap",
        ))
    elif dur and dur > 58:
        issues.append(Issue(
            "warning", "duration_near_limit",
            f"duration {dur:.1f}s (within 2s of the 60s ceiling)",
        ))

    # --- filesize ---
    size_mb = info.get("filesize_mb", 0)
    if size_mb and size_mb > 256:
        issues.append(Issue(
            "warning", "filesize_large",
            f"{size_mb} MB — slower upload, no quality benefit at this length",
        ))

    # --- captions ---
    max_words = captions.get("max_words_per_cue", 0)
    avg_words = captions.get("avg_words_per_cue", 0)
    cue_count = captions.get("cue_count", 0)
    if max_words > 4:
        issues.append(Issue(
            "warning", "captions_too_dense",
            f"max {max_words} words/cue (Netflix-style for vertical: 1-4)",
            "set shorts_mode=True in tts.synthesize_screenplay",
        ))
    elif avg_words > 4:
        issues.append(Issue(
            "warning", "captions_avg_too_dense",
            f"avg {avg_words} words/cue across {cue_count} cues",
            "set shorts_mode=True in tts.synthesize_screenplay",
        ))
    if cue_count == 0:
        issues.append(Issue(
            "warning", "no_captions",
            "no caption cues — most Shorts viewers watch with audio off",
        ))

    critical = any(i.severity == "critical" for i in issues)
    return QualityReport(passed=not critical, metrics=metrics, issues=issues)


def format_report(rep: QualityReport) -> str:
    """Human-readable rendering for state.log."""
    lines: list[str] = [rep.summary(), ""]
    if rep.metrics:
        lines.append("metrics:")
        for k, v in sorted(rep.metrics.items()):
            lines.append(f"  {k}: {v}")
        lines.append("")
    if rep.issues:
        lines.append("issues:")
        for i in rep.issues:
            lines.append(f"  [{i.severity.upper()}] {i.code}: {i.message}")
            if i.fix_hint:
                lines.append(f"      -> {i.fix_hint}")
    return "\n".join(lines)
