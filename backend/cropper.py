import textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps

TARGET_RATIO = 9 / 16
OUTPUT_DIR = Path(__file__).parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

# Named fonts available on macOS
FONTS: dict[str, str] = {
    "Helvetica Neue": "/System/Library/Fonts/HelveticaNeue.ttc",
    "Optima":         "/System/Library/Fonts/Optima.ttc",
    "Futura":         "/Library/Fonts/Futura.ttc",
    "Didot":          "/Library/Fonts/Didot.ttc",
    "Baskerville":    "/Library/Fonts/Baskerville.ttc",
    "Georgia":        "/Library/Fonts/Georgia.ttf",
    "Noteworthy":     "/System/Library/Fonts/Noteworthy.ttc",
    "Palatino":       "/Library/Fonts/Palatino.ttc",
    "Times New Roman":"/Library/Fonts/Times New Roman.ttf",
    "Impact":         "/Library/Fonts/Impact.ttf",
    "Geneva":         "/System/Library/Fonts/Geneva.ttf",
    "Trebuchet":      "/Library/Fonts/Trebuchet MS.ttf",
}

# Curated list for the UI (prettiest subset)
FEATURED_FONTS = [
    "Didot", "Optima", "Futura", "Baskerville",
    "Georgia", "Noteworthy", "Palatino", "Helvetica Neue",
]

def _open_image(path: str) -> Image.Image:
    """Open image and auto-rotate based on EXIF orientation."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)   # fixes rotated photos
    return img.convert("RGB")


def _load_font(size: int, font_name: str = "Helvetica Neue") -> ImageFont.FreeTypeFont:
    import random
    if font_name == "random":
        font_name = random.choice(FEATURED_FONTS)
    path = FONTS.get(font_name)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    # Fallback chain
    for fp in FONTS.values():
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            continue
    return ImageFont.load_default(size=size)


def smart_crop_to_story(
    source_path: str,
    output_filename: str | None = None,
    target_w: int = 1080,
    target_h: int = 1920,
) -> str:
    """Crop + resize image to 9:16 (1080×1920), respecting EXIF orientation."""
    img = _open_image(source_path)
    orig_w, orig_h = img.size
    orig_ratio = orig_w / orig_h

    if abs(orig_ratio - TARGET_RATIO) < 0.01:
        img = img.resize((target_w, target_h), Image.LANCZOS)
    elif orig_ratio > TARGET_RATIO:
        # Landscape → crop sides, keep center
        new_w = int(orig_h * TARGET_RATIO)
        left = (orig_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, orig_h))
        img = img.resize((target_w, target_h), Image.LANCZOS)
    else:
        # Portrait taller than 9:16 → crop top/bottom (bias upward for faces)
        new_h = int(orig_w / TARGET_RATIO)
        top = int((orig_h - new_h) * 0.35)
        img = img.crop((0, top, orig_w, top + new_h))
        img = img.resize((target_w, target_h), Image.LANCZOS)

    if not output_filename:
        output_filename = f"{Path(source_path).stem}_story.jpg"

    out_path = OUTPUT_DIR / output_filename
    img.save(out_path, "JPEG", quality=92)
    return str(out_path)


def add_text_overlay(
    image_path: str,
    text: str,
    style: dict,
    output_filename: str | None = None,
) -> str:
    """
    Overlay styled text on an image.
    style keys:
      position   : "top" | "center" | "bottom"
      font_size  : int (pixels, relative to 1080px wide image)
      color      : "#RRGGBB"
      bg_color   : "#RRGGBB"
      bg_opacity : 0–255
      align      : "left" | "center" | "right"
    """
    img = _open_image(image_path).convert("RGBA")
    w, h = img.size

    # Scale font relative to actual image width so it looks right on any size
    base_font_size = int(style.get("font_size", 72))
    font_size  = max(24, int(base_font_size * w / 1080))
    font_name  = style.get("font", "Helvetica Neue")

    position   = style.get("position", "bottom")
    color      = style.get("color", "#FFFFFF")
    bg_color   = style.get("bg_color", "#000000")
    bg_opacity = int(style.get("bg_opacity", 160))
    align      = style.get("align", "center")

    def hex_to_rgb(hx: str) -> tuple:
        hx = hx.lstrip("#")
        return tuple(int(hx[i:i+2], 16) for i in (0, 2, 4))

    text_rgb = hex_to_rgb(color)
    bg_rgb   = hex_to_rgb(bg_color)
    font     = _load_font(font_size, font_name)

    # Wrap text to fit image width (leave 80px margin each side)
    usable_w = w - 160
    # Estimate chars per line from font metrics
    test_bbox = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), "W", font=font)
    char_w = max(1, test_bbox[2] - test_bbox[0])
    max_chars = max(8, usable_w // char_w)
    lines = textwrap.wrap(text, width=max_chars) or [text]

    line_height  = font_size + int(font_size * 0.2)
    total_text_h = line_height * len(lines)
    padding_v    = int(font_size * 0.6)
    block_h      = total_text_h + padding_v * 2

    # Vertical anchor
    if position == "top":
        block_top = int(h * 0.05)
    elif position == "center":
        block_top = (h - block_h) // 2
    else:  # bottom
        block_top = h - block_h - int(h * 0.05)

    # ── Semi-transparent background strip ─────────────────────────────────────
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    odraw.rectangle(
        [0, block_top, w, block_top + block_h],
        fill=(*bg_rgb, bg_opacity),
    )
    img = Image.alpha_composite(img, overlay)

    # ── Text ──────────────────────────────────────────────────────────────────
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        bbox  = draw.textbbox((0, 0), line, font=font)
        lw    = bbox[2] - bbox[0]
        lh    = bbox[3] - bbox[1]

        if align == "center":
            x = (w - lw) // 2
        elif align == "right":
            x = w - lw - 80
        else:
            x = 80

        y = block_top + padding_v + i * line_height

        # Drop shadow (2px offset)
        shadow_off = max(2, font_size // 30)
        draw.text((x + shadow_off, y + shadow_off), line, font=font, fill=(0, 0, 0, 200))
        # Main text
        draw.text((x, y), line, font=font, fill=(*text_rgb, 255))

    result = img.convert("RGB")

    if not output_filename:
        output_filename = f"{Path(image_path).stem}_text.jpg"

    out_path = OUTPUT_DIR / output_filename
    result.save(out_path, "JPEG", quality=92)
    return str(out_path)
