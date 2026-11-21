"""
Frame-perfect renderer: drives motion/engine.html in HEADLESS Chromium,
screenshots every frame and pipes the JPEGs into ffmpeg, then muxes the
narration (loudness-normalised to YouTube's -14 LUFS).
"""
from __future__ import annotations

import math
import subprocess
import time
from pathlib import Path

import tts

ENGINE = Path(__file__).with_name("engine.html")
LONG_SIZE = (1920, 1080)
SHORT_SIZE = (1080, 1920)


def render_video(timeline: dict, out_mp4: Path, *, vertical: bool, fps: int = 30,
                 handle: str = "@RitikPatill", images: dict[str, str] | None = None,
                 log=None) -> Path:
    from playwright.sync_api import sync_playwright

    w, h = SHORT_SIZE if vertical else LONG_SIZE
    duration = float(timeline["duration"])
    payload = {
        "size": {"w": w, "h": h}, "duration": duration, "handle": handle,
        "scenes": [{k: s[k] for k in ("type", "start", "end", "visual")} for s in timeline["scenes"]],
        "words": timeline["words"], "images": images or {},
        "captions": {"enabled": True, "maxWords": 3 if vertical else 7},
    }
    ff = tts._ffmpeg()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    silent = out_mp4.with_name(out_mp4.stem + ".video.mp4")
    enc = subprocess.Popen(
        [ff, "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(fps), "-c:v", "mjpeg",
         "-i", "-", "-vf", "scale=in_range=pc:out_range=tv,format=yuv420p",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-color_range", "tv",
         "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
         "-r", str(fps), str(silent)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    nframes = int(math.ceil(duration * fps))
    started = time.time()
    recovered = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--hide-scrollbars"])

            def open_page():
                pg = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
                pg.goto(ENGINE.as_uri())
                inf = pg.evaluate("p => window.init(p)", payload)
                pg.evaluate("""() => document.fonts.ready.then(() => Promise.all(
                    [...document.images].map(i => i.complete ? 1 : new Promise(r => { i.onload = i.onerror = r; }))))""")
                return pg, inf

            page, info = open_page()
            for i in range(nframes):
                # Rendering is a pure function of t, so a flaky capture
                # ("Unable to capture screenshot") is retried, and if the
                # page is wedged it is rebuilt and continues at frame i.
                for attempt in range(4):
                    try:
                        page.evaluate("t => window.renderAt(t)", i / fps)
                        buf = page.screenshot(type="jpeg", quality=92)
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        recovered += 1
                        time.sleep(0.5 * (attempt + 1))
                        if attempt >= 1:
                            try:
                                page.close()
                            except Exception:
                                pass
                            page, _ = open_page()
                enc.stdin.write(buf)
            browser.close()
    finally:
        try:
            enc.stdin.close()
        except Exception:
            pass
        enc.wait()
    if enc.returncode != 0 or not silent.exists():
        raise RuntimeError(f"frame encode failed: {enc.stderr.read()[-400:]!r}")
    if log:
        log(f"rendered {nframes} frames ({w}x{h}) in {time.time() - started:.0f}s; {info}"
            + (f"; recovered {recovered} flaky captures" if recovered else ""))
    mux = subprocess.run(
        [ff, "-y", "-v", "error", "-i", str(silent), "-i", str(timeline["audio"]),
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-shortest", "-movflags", "+faststart", str(out_mp4)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if mux.returncode != 0 or not out_mp4.exists():
        raise RuntimeError(f"mux failed: {mux.stderr[-400:]}")
    silent.unlink(missing_ok=True)
    return out_mp4


def snapshot_frames(timeline: dict, out_dir: Path, times: list[float], *, vertical: bool,
                    images: dict[str, str] | None = None, handle: str = "@RitikPatill",
                    clean: bool = False) -> list[Path]:
    """Render individual frames as PNG (for the critic / thumbnails / tests).
    clean=True hides captions and the handle (thumbnail visuals)."""
    from playwright.sync_api import sync_playwright

    w, h = SHORT_SIZE if vertical else LONG_SIZE
    payload = {
        "size": {"w": w, "h": h}, "duration": float(timeline["duration"]),
        "handle": "" if clean else handle,
        "scenes": [{k: s[k] for k in ("type", "start", "end", "visual")} for s in timeline["scenes"]],
        "words": timeline["words"], "images": images or {},
        "captions": {"enabled": not clean, "maxWords": 3 if vertical else 7},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--hide-scrollbars"])
        page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
        page.goto(ENGINE.as_uri())
        page.evaluate("p => window.init(p)", payload)
        page.evaluate("() => document.fonts.ready")
        for t in times:
            fp = out_dir / f"frame_{t:07.2f}.png"
            for attempt in range(4):
                try:
                    page.evaluate("t => window.renderAt(t)", t)
                    page.screenshot(path=str(fp))
                    break
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            paths.append(fp)
        browser.close()
    return paths
