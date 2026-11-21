"""1280x720 custom thumbnail: big text + the video's best visual (headless)."""
from __future__ import annotations

from pathlib import Path

TEMPLATE = Path(__file__).with_name("thumb.html")


def render_thumbnail(*, text: str, emphasis: str, visual_png: Path | None, out_jpg: Path,
                     pill: str = "OPEN SOURCE", handle: str = "@RitikPatill") -> Path:
    from PIL import Image
    from playwright.sync_api import sync_playwright

    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    png = out_jpg.with_suffix(".png")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--hide-scrollbars"])
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        page.goto(TEMPLATE.as_uri())
        page.evaluate("p => window.init(p)", {
            "text": text, "emphasis": emphasis, "pill": pill, "handle": handle,
            "image": visual_png.as_uri() if visual_png and visual_png.exists() else ""})
        page.evaluate("() => document.fonts.ready")
        page.screenshot(path=str(png))
        browser.close()
    Image.open(png).convert("RGB").save(out_jpg, "JPEG", quality=92, optimize=True)
    png.unlink(missing_ok=True)
    return out_jpg


def crop_center(src: Path, dst: Path, frac_w: float = 0.78, frac_h: float = 0.80) -> Path:
    """Crop the content area of a 16:9 frame for use inside the thumbnail."""
    from PIL import Image
    im = Image.open(src)
    w, h = im.size
    cw, ch = int(w * frac_w), int(h * frac_h)
    box = ((w - cw) // 2, (h - ch) // 2, (w + cw) // 2, (h + ch) // 2)
    im.crop(box).save(dst)
    return dst
