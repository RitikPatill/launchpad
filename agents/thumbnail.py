"""
Thumbnail composer.

Programmatic thumbnail generation using PIL — NOT AI image generation.
Why deterministic: AI image gen for thumbnails is wildly inconsistent
(every video looks different = no channel branding) and burns quota for
no benefit. PIL composite of "key frame from the recording + project
title overlaid in big readable text + handle" gives us:

  - consistent channel aesthetic across videos (recognizable feed)
  - $0 cost
  - <5 seconds to generate
  - sharp at 1280x720 (YouTube's preferred resolution)

The key frame is sampled from the recorded .webm at ~5 seconds in,
which is past the initial page-load and into the actual content.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _extract_keyframe(video_path: Path, out_path: Path, at_seconds: float = 5.0) -> Path:
    """Pull a single frame from the video at `at_seconds` as a PNG."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(), "-y",
        "-ss", str(at_seconds),
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "2",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"ffmpeg keyframe extraction failed: {proc.stderr[-500:]}")
    return out_path


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Try a few font paths Windows ships with. Fall back to default."""
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf",   # Segoe UI Bold — clean default
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont,
               max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Greedy word-wrap by measured pixel width."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def compose_thumbnail(*, video_path: Path, title: str, handle: str,
                      output_path: Path) -> Path:
    """
    Build a 1280x720 thumbnail:
      - key frame from the recording, dimmed
      - bottom band with project title (big) + handle (small)
      - subtle vignette so the text reads cleanly

    Designed for technical content — clean, no marketing language, no
    arrows or red circles. Recognizable channel aesthetic.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    keyframe = output_path.parent / f"keyframe-{output_path.stem}.png"
    _extract_keyframe(video_path, keyframe)

    base = Image.open(keyframe).convert("RGB").resize((1280, 720), Image.LANCZOS)

    # Dim the whole frame slightly so text reads.
    overlay = Image.new("RGB", base.size, (0, 0, 0))
    base = Image.blend(base, overlay, alpha=0.35)

    # Bottom 40% gets a darker gradient band for the text.
    band = Image.new("RGBA", (1280, 320), (0, 0, 0, 200))
    band = band.filter(ImageFilter.GaussianBlur(radius=2))
    composed = base.convert("RGBA")
    composed.paste(band, (0, 400), band)

    draw = ImageDraw.Draw(composed)

    # Title — big bold, fits up to 3 lines.
    title_font = _load_font(72)
    title_lines = _wrap_text(title, title_font, max_width=1180, draw=draw)
    if len(title_lines) > 3:
        # Drop font size if title is too long for 3 lines.
        title_font = _load_font(56)
        title_lines = _wrap_text(title, title_font, max_width=1180, draw=draw)
    line_h = title_font.size + 12
    y = 430
    for line in title_lines[:3]:
        draw.text((50, y), line, fill=(255, 255, 255), font=title_font,
                  stroke_width=2, stroke_fill=(0, 0, 0))
        y += line_h

    # Handle in the corner — smaller, muted.
    handle_font = _load_font(32)
    draw.text((50, 670), handle, fill=(220, 220, 220), font=handle_font,
              stroke_width=1, stroke_fill=(0, 0, 0))

    composed = composed.convert("RGB")
    composed.save(output_path, "JPEG", quality=92, optimize=True)
    keyframe.unlink(missing_ok=True)
    return output_path
