"""
Video editor / polish agent. Adds production polish to the raw recording
before upload — all free via PIL + bundled ffmpeg, no third-party tools.

What it adds:
  - 2-second intro card (project title + handle, clean dark theme)
  - 2-second outro card (CTA back to your GitHub)
  - Burned-in SRT captions on the main section (when SRT is available)
  - Concatenation: [intro] -> [main with captions] -> [outro]

What it deliberately doesn't add (yet):
  - Crossfade transitions: ffmpeg xfade chain is finicky and adds render
    time without huge visual benefit on tutorial content.
  - Background music: licensing for "free" tracks is murky, especially
    for monetized YouTube channels. Skip until you want to invest in a
    tagged-as-safe library.
  - Animated zooms / pan: fun but turns 30s of render time into 3 minutes.

Total polish overhead: ~30 seconds extra render time per video.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import VIDEO_RESOLUTION


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in (
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap(text: str, font, max_w: int, draw) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _make_card(title_text: str, sub_text: str, out_path: Path,
               accent_color: tuple = (90, 130, 220)) -> Path:
    """Generic card composer — used for both intro and outro."""
    W, H = VIDEO_RESOLUTION
    img = Image.new("RGB", (W, H), (12, 14, 22))

    # Subtle accent gradient on left edge for visual interest.
    gradient = Image.new("RGB", (W, H), (12, 14, 22))
    gd = ImageDraw.Draw(gradient)
    for x in range(0, 12):
        a = int(255 * (1 - x / 12))
        gd.line([(x, 0), (x, H)], fill=(*accent_color, a)[:3])
    img.paste(gradient, (0, 0))

    draw = ImageDraw.Draw(img)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Title — big bold, centered vertically high.
    title_font = _load_font(88)
    title_lines = _wrap(title_text, title_font, max_w=W - 200, draw=draw)
    if len(title_lines) > 2:
        title_font = _load_font(64)
        title_lines = _wrap(title_text, title_font, max_w=W - 200, draw=draw)
    line_h = title_font.size + 12
    y = H // 2 - (line_h * len(title_lines)) // 2 - 20
    for line in title_lines[:3]:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        tx = (W - bbox[2]) // 2
        draw.text((tx, y), line, fill=(240, 240, 240), font=title_font)
        y += line_h

    # Subtitle — smaller, accent color, centered.
    sub_font = _load_font(40)
    bbox = draw.textbbox((0, 0), sub_text, font=sub_font)
    sx = (W - bbox[2]) // 2
    draw.text((sx, y + 24), sub_text, fill=accent_color, font=sub_font)

    img.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path


def make_intro_card(project_title: str, handle: str, out_path: Path) -> Path:
    return _make_card(project_title, handle, out_path)


def make_outro_card(handle: str, out_path: Path) -> Path:
    return _make_card("Find more on GitHub", handle, out_path,
                      accent_color=(110, 200, 140))


def _card_to_clip(card_path: Path, out_clip: Path, duration_s: float = 2.0,
                  fps: int = 24) -> Path:
    """Convert a still image to a short silent video clip."""
    cmd = [
        _ffmpeg(), "-y",
        "-loop", "1", "-i", str(card_path),
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(duration_s),
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(out_clip),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out_clip.exists():
        raise RuntimeError(f"card->clip failed: {proc.stderr[-500:]}")
    return out_clip


def make_short(*, source_long_video: Path, output_path: Path,
               max_seconds: int = 55) -> Path:
    """
    Generate a YouTube Shorts variant from the long-form video:
      - crop horizontal 16:9 -> vertical 9:16 (1080x1920)
      - trim to max_seconds (Shorts must be < 60s)
      - center-crop horizontally so the focal subject (usually middle of
        the page) stays in frame

    YouTube auto-detects vertical + duration < 60s and routes the upload
    to the Shorts shelf. We additionally tag the title/description with
    "#Shorts" at the upload step.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(), "-y",
        "-i", str(source_long_video),
        "-t", str(max_seconds),
        # Center-crop the source to a 9:16 aspect, then scale to 1080x1920.
        # `crop=ih*9/16:ih` keeps full height, takes a centered slice the
        # right width. Then scale ensures clean 1080p vertical.
        "-vf", "crop=ih*9/16:ih,scale=1080:1920",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"ffmpeg make_short failed: {proc.stderr[-500:]}")
    return output_path


def polish_video(*, main_video: Path, srt: Path | None,
                 project_title: str, handle: str,
                 output_path: Path) -> Path:
    """
    Concat: [intro 2s] -> [main with captions burned] -> [outro 2s].
    Captions only burn if `srt` is provided and exists; otherwise main
    is concat'd as-is.
    """
    work_dir = output_path.parent / "polish_tmp"
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate intro + outro card images, then convert to clips.
    intro_jpg = work_dir / "intro_card.jpg"
    outro_jpg = work_dir / "outro_card.jpg"
    make_intro_card(project_title, handle, intro_jpg)
    make_outro_card(handle, outro_jpg)

    intro_clip = work_dir / "intro_clip.mp4"
    outro_clip = work_dir / "outro_clip.mp4"
    _card_to_clip(intro_jpg, intro_clip, duration_s=2.0)
    _card_to_clip(outro_jpg, outro_clip, duration_s=2.5)

    # 2. Burn captions onto the main video if SRT exists.
    if srt and srt.exists():
        main_with_captions = work_dir / "main_with_captions.mp4"
        # Path needs forward slashes + escaped colon for ffmpeg's
        # subtitles filter on Windows.
        srt_for_ffmpeg = str(srt).replace("\\", "/").replace(":", "\\:")
        style = (
            "FontName=Segoe UI,"
            "FontSize=18,"
            "PrimaryColour=&HFFFFFF&,"
            "OutlineColour=&H80000000&,"
            "BackColour=&HA0000000&,"
            "Outline=1,"
            "Shadow=0,"
            "BorderStyle=4,"
            "Alignment=2,"
            "MarginV=40"
        )
        cmd = [
            _ffmpeg(), "-y",
            "-i", str(main_video),
            "-vf", f"subtitles='{srt_for_ffmpeg}':force_style='{style}'",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "copy",
            str(main_with_captions),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode == 0 and main_with_captions.exists():
            main_to_use = main_with_captions
        else:
            # Caption burn-in failed (font issue / path issue). Fall
            # back to the un-burned main; YouTube uploads SRT anyway.
            main_to_use = main_video
    else:
        main_to_use = main_video

    # 3. Concat intro + main + outro using the concat demuxer.
    concat_list = work_dir / "concat.txt"
    concat_list.write_text(
        f"file '{intro_clip.as_posix()}'\n"
        f"file '{main_to_use.as_posix()}'\n"
        f"file '{outro_clip.as_posix()}'\n",
        encoding="utf-8",
    )
    cmd = [
        _ffmpeg(), "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not output_path.exists():
        # Fallback: just copy the un-polished main if concat fails.
        import shutil
        shutil.copyfile(main_video, output_path)
    return output_path
