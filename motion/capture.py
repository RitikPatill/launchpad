"""Real screenshots of the project's public pages (headless Chromium, dark mode)."""
from __future__ import annotations

from pathlib import Path


def repo_screen(url: str, out_png: Path, *, width: int = 1280, max_height: int = 3600) -> Path | None:
    """Screenshot the top of a public page (e.g. the GitHub repo). Returns the
    PNG path or None if the page could not be captured (never raises)."""
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        from playwright.sync_api import sync_playwright
        out_png.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--hide-scrollbars"])
            page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme="dark",
                                    device_scale_factor=1)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)
            # Hide sticky banners/headers that would cover the scrolled content.
            page.add_style_tag(content="header, .js-header-wrapper, .flash-full, .cookie-banner, "
                                       "[data-testid='cookie-banner'] { display: none !important; }")
            h = min(max_height, page.evaluate("() => document.documentElement.scrollHeight"))
            page.screenshot(path=str(out_png), full_page=True,
                            clip={"x": 0, "y": 0, "width": width, "height": h})
            browser.close()
        return out_png if out_png.exists() else None
    except Exception:
        return None
