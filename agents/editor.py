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

import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from config import SHORTS_RESOLUTION, VIDEO_RESOLUTION


# --- SRT -> ASS converter (used by polish_short) ----------------------- #
#
# Why this exists: ffmpeg's `subtitles` filter converts SRT to ASS
# internally with libass defaults (PlayResY=288). With a 1080x1920 video,
# every value in force_style gets scaled by 1920/288 = 6.67x — which is
# why our previous attempts had captions rendering ~150 px tall and
# anchored above the middle of the frame.
#
# Solving it at the source: emit a real .ass file with `PlayResY=1920`
# in the [Script Info] header. Now every FontSize/MarginV/Outline value
# is in real screen pixels. No more scaling math.

def _srt_time_to_ass(srt_t: str) -> str:
    """`00:01:23,456`  ->  `0:01:23.45`  (ASS uses centiseconds)."""
    m = re.match(r"(\d+):(\d+):(\d+),(\d+)", srt_t.strip())
    if not m:
        return "0:00:00.00"
    h, mm, ss, ms = m.groups()
    cs = int(ms) // 10
    return f"{int(h)}:{mm}:{ss}.{cs:02d}"


def _srt_to_ass_shorts(srt_path: Path, ass_path: Path) -> Path:
    """Convert SRT -> ASS with Netflix-style header baked in.

    Sizing (researched against Netflix/BBC/TikTok 2026 norms):
      - FontSize 64 = 3.3% of 1920 (Netflix "restrained" band 3-4%)
      - MarginV 240 = ~12.5% from bottom — clears YouTube Shorts'
        bottom UI (channel chip + progress + ~safety margin)
      - Outline 4 + Shadow 2 = readable on any frame without a box
      - Alignment 2 = bottom-center anchor
      - Bold (-1 in ASS = true)
    """
    srt_text = srt_path.read_text(encoding="utf-8", errors="replace")

    cues: list[tuple[str, str, str]] = []
    lines = srt_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        # Cue index line (just digits) — skip; the next line is the timecode.
        if line.isdigit() and (i + 1) < len(lines) and "-->" in lines[i + 1]:
            i += 1
            continue
        if "-->" in line:
            try:
                left, right = [p.strip() for p in line.split("-->", 1)]
                start = _srt_time_to_ass(left)
                end = _srt_time_to_ass(right)
            except Exception:
                i += 1
                continue
            i += 1
            text_lines: list[str] = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].rstrip())
                i += 1
            # ASS uses `\N` for hard line break; commas in text are fine
            # because Text is the last (open-ended) field of Dialogue.
            text = "\\N".join(text_lines)
            if text:
                cues.append((start, end, text))
            continue
        i += 1

    header = (
        "[Script Info]\n"
        "Title: shorts-captions\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"                 # 2 = no auto-wrap (we control line breaks)
        "ScaledBorderAndShadow: yes\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"               # <-- the whole point: real screen pixels
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # White text, black outline, no fill box. Bold=-1 = ON in ASS.
        "Style: Default,Segoe UI,64,&H00FFFFFF,&H00FFFFFF,"
        "&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,4,2,"
        "2,80,80,240,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    body_lines = [
        f"Dialogue: 0,{s},{e},Default,,0,0,0,,{t}" for (s, e, t) in cues
    ]
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(header + "\n".join(body_lines) + "\n",
                        encoding="utf-8")
    return ass_path


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


def _make_hook_card_vertical(title_text: str, out_path: Path) -> Path:
    """Build a 1080x1920 hook card: dark dramatic background + huge bold
    project title + small accent bar. Used as the first 2 seconds of a
    Short to give the swipe-stopping moment the screen recording lacks."""
    W, H = SHORTS_RESOLUTION  # 1080x1920
    img = Image.new("RGB", (W, H), (8, 10, 18))

    # Vertical dramatic gradient: deep navy bottom -> brighter accent top.
    # Subtle, just enough to feel produced. Done by drawing horizontal bands.
    grad = Image.new("RGB", (W, H), (8, 10, 18))
    gd = ImageDraw.Draw(grad)
    for y in range(0, H):
        t = y / H
        r = int(8 + (32 - 8) * (1 - t))
        g = int(10 + (54 - 10) * (1 - t))
        b = int(18 + (110 - 18) * (1 - t))
        gd.line([(0, y), (W, y)], fill=(r, g, b))
    img.paste(grad, (0, 0))

    draw = ImageDraw.Draw(img)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Top accent label.
    label_font = _load_font(56)
    label = "BUILT AUTONOMOUSLY"
    bbox = draw.textbbox((0, 0), label, font=label_font)
    lx = (W - bbox[2]) // 2
    draw.text((lx, int(H * 0.20)), label, fill=(120, 170, 255), font=label_font)

    # Big bold project title — fills middle band. Auto-shrink on overflow.
    title_size = 130
    title_font = _load_font(title_size)
    title_lines = _wrap(title_text, title_font, max_w=W - 140, draw=draw)
    while len(title_lines) > 3 and title_size > 70:
        title_size -= 12
        title_font = _load_font(title_size)
        title_lines = _wrap(title_text, title_font, max_w=W - 140, draw=draw)

    line_h = title_font.size + 16
    total_h = line_h * len(title_lines[:4])
    y = H // 2 - total_h // 2
    for line in title_lines[:4]:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        tx = (W - bbox[2]) // 2
        # Soft shadow for depth.
        draw.text((tx + 4, y + 4), line, fill=(0, 0, 0), font=title_font)
        draw.text((tx, y), line, fill=(245, 245, 250), font=title_font)
        y += line_h

    # Bottom accent bar with a small CTA.
    bar_y = int(H * 0.82)
    draw.rectangle([(W // 2 - 220, bar_y), (W // 2 + 220, bar_y + 6)],
                   fill=(120, 170, 255))
    cta_font = _load_font(44)
    cta = "Watch the build"
    bbox = draw.textbbox((0, 0), cta, font=cta_font)
    cx = (W - bbox[2]) // 2
    draw.text((cx, bar_y + 26), cta, fill=(220, 230, 255), font=cta_font)

    img.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path


def make_hook_clip_vertical(title_text: str, out_path: Path,
                             duration_s: float = 2.0) -> Path:
    """Render the vertical hook card as a 2s 1080x1920 silent clip with a
    very subtle zoom-in (zoompan) for energy. Returns the clip path.
    Failures bubble up so callers can fall back to the existing
    no-hook polish path."""
    work_dir = out_path.parent / "_hook_tmp"
    work_dir.mkdir(parents=True, exist_ok=True)
    card_jpg = work_dir / "hook_card.jpg"
    _make_hook_card_vertical(title_text, card_jpg)

    # zoompan: a slow 1.0 -> 1.06 zoom over 'duration_s' seconds keeps
    # the static card alive without being distracting. d=24*duration_s
    # frames at 24fps. setsar to keep the result a clean 1080x1920.
    fps = 24
    frames = int(fps * duration_s)
    cmd = [
        _ffmpeg(), "-y",
        "-loop", "1", "-i", str(card_jpg),
        # Match the main short's audio params exactly (48k stereo) so the
        # concat re-encode doesn't have to resample between clips — that
        # mismatch was a contributor to the prior audio distortion.
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(duration_s),
        "-r", str(fps),
        "-vf", (
            f"zoompan=z='min(zoom+0.0015,1.06)':d={frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920,"
            f"setsar=1"
        ),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"hook clip render failed: {proc.stderr[-500:]}")
    return out_path


def polish_short(*, main_video: Path, srt: Path | None,
                 output_path: Path,
                 hook_title: str | None = None) -> Path:
    """
    Polish a vertical Short. Different priorities than long-form:
      - NO intro/outro cards — every second matters in a 30-50s video
      - BIG burned-in captions (font size ~36, center-bottom, opaque
        background) because Shorts are often watched silently
      - Light noise reduction skipped (waste of render time at this length)

    Optional `hook_title`: when provided, a 2-second branded vertical hook
    card is prepended to the Short. Pure ffmpeg+PIL — no external assets,
    no API calls. If hook generation or concat fails for ANY reason, the
    function silently falls back to the legacy no-hook output: existing
    behavior is fully preserved.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: build the captioned Short the way we always have. We write
    # to an intermediate file IF a hook will be prepended, otherwise we
    # write directly to output_path (zero overhead vs old behavior).
    captioned_intermediate = (
        output_path.parent / "_captioned_pre_hook.mp4" if hook_title else output_path
    )

    if not srt or not srt.exists():
        cmd = [
            _ffmpeg(), "-y", "-i", str(main_video),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "copy",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(captioned_intermediate),
        ]
    else:
        # Convert SRT -> ASS with a 1080x1920 PlayRes header so libass
        # doesn't scale our values by 6.67x. force_style is NOT used —
        # the style is baked into the ASS Style line itself, which means
        # PlayResY=1920 takes effect (force_style values get re-scaled
        # by libass even when PlayRes is overridden in the script).
        ass_path = captioned_intermediate.parent / "_shorts_captions.ass"
        _srt_to_ass_shorts(srt, ass_path)
        ass_for_ffmpeg = str(ass_path).replace("\\", "/").replace(":", "\\:")
        cmd = [
            _ffmpeg(), "-y", "-i", str(main_video),
            "-vf", f"subtitles='{ass_for_ffmpeg}'",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "copy",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(captioned_intermediate),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not captioned_intermediate.exists():
        # Fallback: copy un-polished if burn-in failed.
        import shutil
        shutil.copyfile(main_video, captioned_intermediate)

    # If no hook requested, we're done — captioned_intermediate is already
    # output_path.
    if not hook_title:
        return output_path

    # Step 2: try to build & prepend the hook. Any failure -> fall back to
    # the captioned-only output (existing behavior preserved).
    work_dir = output_path.parent / "_hook_tmp"
    try:
        hook_clip = work_dir / "hook_clip.mp4"
        make_hook_clip_vertical(hook_title, hook_clip, duration_s=2.0)

        # Re-encode concat (not stream-copy) — the hook and main may
        # have slightly different SAR/timebase; re-encoding sidesteps
        # all of those compatibility issues at the cost of one pass.
        concat_list = work_dir / "concat.txt"
        concat_list.write_text(
            f"file '{hook_clip.as_posix()}'\n"
            f"file '{captioned_intermediate.as_posix()}'\n",
            encoding="utf-8",
        )
        cmd = [
            _ffmpeg(), "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            # Uniform 48k stereo throughout. Hook and main both arrive
            # at 48k stereo (compose normalized them); explicit params
            # here lock the re-encode so the demuxer doesn't pick weird
            # bitrates as it did previously (20 kb/s output despite
            # requesting 128k).
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not output_path.exists():
            raise RuntimeError(f"hook concat failed: {proc.stderr[-500:]}")
    except Exception:
        # Silent fallback to the captioned-only output. The pipeline
        # still ships a valid Short — just without the hook.
        import shutil
        if captioned_intermediate.exists():
            shutil.copyfile(captioned_intermediate, output_path)
        else:
            shutil.copyfile(main_video, output_path)

    # Cleanup: remove the captioned intermediate (we only kept it to
    # support fallback). Best-effort — don't break on permission errors.
    try:
        if captioned_intermediate != output_path and captioned_intermediate.exists():
            captioned_intermediate.unlink()
    except Exception:
        pass
    return output_path


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
