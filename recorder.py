"""
Screen recorder. Uses Playwright to launch a Chromium browser, execute
the screenplay's actions step-by-step, and capture the whole session as
a video file.

Why Playwright instead of OBS / ffmpeg-grab: Playwright lets us script
exact deterministic actions (goto, scroll, wait) — every video for the
same project comes out identical. OBS would require manual capture
windows and would pick up our desktop wallpaper.

Output: an .webm file. The compositor converts to .mp4 at mux time.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from config import VIDEO_FPS, VIDEO_RESOLUTION


async def _execute_action(page, action: dict) -> None:
    t = action.get("type")
    if t == "goto":
        url = action["url"]
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            # network hiccup — try again with looser wait
            await page.goto(url, wait_until="load", timeout=30000)
        # Give web apps a moment to render JS-heavy frames.
        await page.wait_for_timeout(1500)
    elif t == "wait":
        await page.wait_for_timeout(int(action.get("seconds", 1) * 1000))
    elif t == "scroll":
        y = action.get("y", 400)
        duration = action.get("duration_ms", 2000)
        # Smooth scroll via JS — plays back nicely on video.
        await page.evaluate(
            "([y, duration]) => new Promise(r => {"
            "  const start = window.scrollY;"
            "  const t0 = performance.now();"
            "  function step(t) {"
            "    const k = Math.min(1, (t - t0) / duration);"
            "    const e = k < 0.5 ? 2*k*k : -1 + (4-2*k)*k;"  # ease in-out quad
            "    window.scrollTo(0, start + y * e);"
            "    if (k < 1) requestAnimationFrame(step);"
            "    else r();"
            "  }"
            "  requestAnimationFrame(step);"
            "})",
            [y, duration],
        )
        await page.wait_for_timeout(300)
    elif t == "click":
        try:
            await page.click(action["selector"], timeout=5000)
        except Exception:
            pass  # don't crash the recording over a missed click


async def _record_async(scenes: list, output_dir: Path) -> Path:
    from playwright.async_api import async_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ])
        context = await browser.new_context(
            viewport={"width": VIDEO_RESOLUTION[0], "height": VIDEO_RESOLUTION[1]},
            record_video_dir=str(output_dir),
            record_video_size={"width": VIDEO_RESOLUTION[0], "height": VIDEO_RESOLUTION[1]},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
        )
        page = await context.new_page()
        # Friendly intro hold so the video doesn't start mid-load.
        await page.wait_for_timeout(500)
        for scene in scenes:
            for action in scene.get("actions", []):
                await _execute_action(page, action)
        # Friendly closing hold.
        await page.wait_for_timeout(1000)
        await context.close()
        await browser.close()

    # Playwright writes a single .webm into output_dir; find and return it.
    webms = sorted(output_dir.glob("*.webm"), key=lambda p: p.stat().st_mtime)
    if not webms:
        raise RuntimeError("recorder produced no output webm")
    return webms[-1]


def record(screenplay: dict, output_dir: Path) -> Path:
    """Synchronous entry point. Returns path to the captured .webm."""
    return asyncio.run(_record_async(screenplay["scenes"], output_dir))
