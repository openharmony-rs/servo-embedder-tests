"""Pixel checks on device screenshots."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

# (left, top, right, bottom) in screen pixels, like `ui.Node.bounds`.
Box = tuple[int, int, int, int]
# `vertical_shift` compares screenshots at 1/SHIFT_SCALE of their size, so it measures in steps of SHIFT_SCALE px.
SHIFT_SCALE = 4
# A row matches when its mean luminance difference (0-255) is at most this; JPEG noise stays under 3.
ROW_MATCH = 6
# Rows whose luminance varies less than this (standard deviation) are flat and match at any shift, so they don't count.
ROW_TEXTURE = 8
# Moved rows that make a shift. Scrolled pages measured 59 or more, unrelated or unchanged ones up to 36.
MIN_MOVED_ROWS = 48


def _luminance(path: Path, box: Box, scale: int = 1) -> Image.Image:
    with Image.open(path) as image:
        cropped = image.convert("L").crop(box)
    if scale == 1:
        return cropped
    return cropped.resize((cropped.width // scale, cropped.height // scale), Image.Resampling.BOX)


def contrast(path: Path, box: Box) -> float:
    """Standard deviation of the luminance in `box`, 0-255: close to 0 where the screen shows one flat colour."""
    return ImageStat.Stat(_luminance(path, box)).stddev[0]


def difference(a: Path, b: Path, box: Box) -> float:
    """Mean absolute luminance difference of two screenshots in `box`, 0-255."""
    return ImageStat.Stat(ImageChops.difference(_luminance(a, box), _luminance(b, box))).mean[0]


def vertical_shift(a: Path, b: Path, box: Box, limit: int) -> int | None:
    """How far the content in `box` moved down from screenshot `a` to `b` (negative: up), up to `limit` pixels.

    Finds the shift under which most rows of `a` that have texture reappear in `b`, not counting rows that match
    unshifted, such as a sticky header. Rows that change, such as images loading, don't count either. 0 if nothing
    moved, None if the content changed in a way no shift up to `limit` explains, e.g. a different page.
    """
    before, after = _luminance(a, box, SHIFT_SCALE), _luminance(b, box, SHIFT_SCALE)
    textured = [
        ImageStat.Stat(before.crop((0, y, before.width, y + 1))).stddev[0] >= ROW_TEXTURE for y in range(before.height)
    ]
    still = _matching_rows(before, after, 0, textured)
    limit = min(limit // SHIFT_SCALE, before.height // 2)
    shifts = [shift for shift in range(-limit, limit + 1) if shift]
    moved = {shift: len(_matching_rows(before, after, shift, textured) - still) for shift in shifts}
    best = max(shifts, key=lambda shift: (moved[shift], -abs(shift)), default=0)
    if best and moved[best] >= MIN_MOVED_ROWS:
        return best * SHIFT_SCALE
    return 0 if len(still) * 2 >= sum(textured) else None


def _matching_rows(before: Image.Image, after: Image.Image, shift: int, textured: list[bool]) -> set[int]:
    """The rows of `after` that show a textured row of `before`, moved down by `shift`."""
    width, height = before.size
    start, end, rows = max(-shift, 0), max(shift, 0), height - abs(shift)
    moved = before.crop((0, start, width, start + rows))
    current = after.crop((0, end, width, end + rows))
    diffs = ImageChops.difference(moved, current).resize((1, rows), Image.Resampling.BOX).tobytes()
    return {end + y for y, diff in enumerate(diffs) if diff <= ROW_MATCH and textured[start + y]}


def contact_sheet(steps: list[tuple[str, Path]], dest: Path, width: int = 270) -> Path:
    """Puts the screenshots of `steps`, (name, path) pairs, side by side under their names, scaled to `width`."""
    images = []
    for _, path in steps:
        with Image.open(path) as image:
            images.append(image.convert("RGB").resize((width, image.height * width // image.width)))
    label = 24
    sheet = Image.new("RGB", (len(images) * (width + 8), max(i.height for i in images) + label), "white")
    draw = ImageDraw.Draw(sheet)
    for i, ((name, _), image) in enumerate(zip(steps, images, strict=True)):
        sheet.paste(image, (i * (width + 8), label))
        draw.text((i * (width + 8) + 2, 4), name, fill="black")
    sheet.save(dest, quality=85)
    return dest
