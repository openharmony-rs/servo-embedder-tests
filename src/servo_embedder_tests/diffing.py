"""Comparing two captures of the same page: where they differ, how much, and what is there.

Captures are screenshots of a viewport with a known size in CSS pixels. Pixels differ when a channel differs by more
than a tolerance; the image is split into tiles, and a tile differs when more than a share of its pixels differ. A
count over the whole image would dilute a small but real change, such as one heading drawn in a fallback font on a
long page. Neighbouring differing tiles form regions, which are reported in CSS pixels with what the DOM snapshots
have there.
"""

from __future__ import annotations

import colorsys
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

# (left, top, right, bottom) in CSS pixels of the viewport.
CssBox = tuple[float, float, float, float]
# Regions that get a crop for review and measurements, the largest first.
MAX_CROPS = 8
# CSS pixels of context around a region in its crop.
CROP_MARGIN = 8
# Crops are enlarged up to this factor, and to at most this width.
CROP_ZOOM = 4
CROP_WIDTH = 1024
# Measurements: how far, in CSS px, to look for an offset that aligns the renderings, and the radius, in CSS px, of the
# median filter that estimates the background behind text (wider than strokes, so that it keeps gradients only).
MAX_OFFSET = 6
BACKGROUND_RADIUS = 10
# A pixel is ink when its luminance differs from the background by more than this (0-255).
INK_CONTRAST = 48
# Strips, in CSS px, that are aligned one by one to tell letter spacing from other glyphs, and how far, in CSS px, each
# looks around the previous one's offset.
STRIP = 24
STRIP_SEARCH = 3
# Edge detector output above which a pixel is on an edge. Anti-aliasing changes pixels on edges both renderings have;
# on the DAYU200, 69-100% of the differing pixels of such regions, against 2-14% for a missing texture or picture.
EDGE_CONTRAST = 24
# Colour comparison: a pixel is coloured when its channels spread by at least COLOUR_CHROMA and its brightest is at
# least 64; hues fall into 12 bins of 30 degrees. A colour counts as changed where a rendering has COLOUR_AREA CSS px²
# of a hue with no similar hue within COLOUR_REACH CSS px in the other, which shows a colour other than the
# background around them there. On 142 regions of eight GenUI pages against ArkWeb, that found the colour emoji drawn
# black and red hearts drawn pink, and nothing in regions of thinner strokes, other glyphs or resampled photos.
COLOUR_CHROMA = 48
COLOUR_AREA = 40
COLOUR_REACH = 2
HUES = (
    "red",
    "orange",
    "yellow",
    "yellow-green",
    "green",
    "spring green",
    "cyan",
    "azure",
    "blue",
    "violet",
    "purple",
    "pink",
)


@dataclass(frozen=True)
class Preset:
    """How strict a comparison is.

    `tolerance` is the largest channel difference (0-255) of pixels that count as equal, `tile` the tile size in CSS
    pixels, `tile_share` the share of a tile's pixels that may differ. `css` compares at CSS pixel resolution, which
    averages out anti-aliasing differences between engines; otherwise captures of the same size are compared at their
    own resolution.
    """

    tolerance: int
    tile: int
    tile_share: float
    css: bool

    def updated(self, values: dict[str, object]) -> Preset:
        return Preset(**{**asdict(self), **values})  # type: ignore[arg-type]


# The same engine on the same device: renderings should be equal but for noise. Measured on a DAYU200 (DPR 1.5, tiles
# of 576 px) over 133 captures of GenUI pages: the largest tile differed in 18 pixels at tolerance 12 (a 1 px image
# edge), while one element in another font family, with letter-spacing 0.3px or moved by 1 px gave tiles of 131 pixels
# or more. A faint background texture that went missing changed pixels by up to 17, hence not a higher tolerance.
BASELINE = Preset(tolerance=12, tile=16, tile_share=0.05, css=False)
# Different engines: anti-aliasing and rasterization differ everywhere, so only clear differences count.
REFERENCE = Preset(tolerance=48, tile=16, tile_share=0.05, css=True)


@dataclass
class Region:
    """Neighbouring differing tiles."""

    box: CssBox
    tiles: int
    differing: int
    mean_difference: float
    max_difference: int
    elements: list[dict[str, object]] = field(default_factory=list)
    reference: str | None = None
    crop: str | None = None
    # Pixel measurements (see `measure`) and what they mean.
    measured: dict[str, object] | None = None
    reading: str | None = None


@dataclass
class Diff:
    """Region boxes are in CSS pixels of the actual capture's viewport; the compared area starts `offset` CSS pixels
    below its top, `top` in the document."""

    size: tuple[int, int]
    top: float
    offset: float
    scale: float
    tiles: int
    differing_tiles: int
    differing: int
    max_difference: int
    regions: list[Region]
    preset: Preset
    notes: list[str] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        return self.differing_tiles == 0

    def summary(self) -> str:
        if self.matches:
            return "matches"
        largest = max(self.regions, key=lambda region: region.differing)
        left, top, right, bottom = (round(value) for value in largest.box)
        return (
            f"{self.differing_tiles} of {self.tiles} tiles differ in {len(self.regions)} region(s), the largest at "
            f"({left}, {top})-({right}, {bottom})"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "matches": self.matches,
            "summary": self.summary(),
            "size": list(self.size),
            "offset": self.offset,
            "scale": self.scale,
            "tiles": self.tiles,
            "differing_tiles": self.differing_tiles,
            "differing_pixels": self.differing,
            "max_difference": self.max_difference,
            "preset": asdict(self.preset),
            "notes": self.notes,
            "regions": [asdict(region) for region in self.regions],
        }


@dataclass(frozen=True)
class Shot:
    """A capture: its image, the size of the viewport it shows in CSS pixels, how far the document was scrolled down
    and the boxes to ignore, in CSS pixels of the viewport."""

    path: Path
    size: tuple[int, int]
    scroll: float = 0.0
    masks: tuple[CssBox, ...] = ()

    def load(self, top: float, size: tuple[int, int]) -> Image.Image:
        """The `size` CSS pixels of the document from `top` down, at the image's resolution."""
        with Image.open(self.path) as image:
            rgb = image.convert("RGB")
        scale = rgb.width / self.size[0]
        start = top - self.scroll
        return rgb.crop((0, round(start * scale), round(size[0] * scale), round((start + size[1]) * scale)))


def _masked(image: Image.Image, shots: Sequence[Shot], top: float, scale: float) -> Image.Image:
    """`image`, the document from `top` down at `scale`, with the masks of `shots` filled in."""
    boxes = [
        (left, upper + shot.scroll, right, lower + shot.scroll)
        for shot in shots
        for left, upper, right, lower in shot.masks
    ]
    if not boxes:
        return image
    image = image.copy()
    draw = ImageDraw.Draw(image)
    for left, upper, right, lower in boxes:
        draw.rectangle(
            (left * scale, (upper - top) * scale, right * scale - 1, (lower - top) * scale - 1), fill=(255, 0, 255)
        )
    return image


def window(expected: Shot, actual: Shot) -> tuple[float, tuple[int, int]]:
    """The top, in the document, and the size of the area both shots show, in CSS pixels."""
    top = max(expected.scroll, actual.scroll)
    bottom = min(expected.scroll + expected.size[1], actual.scroll + actual.size[1])
    return top, (min(expected.size[0], actual.size[0]), max(0, int(bottom - top)))


def _prepare(
    expected: Shot, actual: Shot, preset: Preset
) -> tuple[Image.Image, Image.Image, float, float, tuple[int, int], list[str]]:
    notes = []
    top, size = window(expected, actual)
    if expected.size != actual.size or expected.scroll != actual.scroll:
        notes.append(
            f"expected shows {expected.size[0]}x{expected.size[1]} CSS px from {expected.scroll:g} down, actual "
            f"{actual.size[0]}x{actual.size[1]} from {actual.scroll:g}; compared the {size[0]}x{size[1]} from "
            f"{top:g} that both show"
        )
    if size[1] == 0:
        raise ValueError("the captures show no common part of the page")
    a, b = expected.load(top, size), actual.load(top, size)
    if preset.css or a.size != b.size:
        if a.size != b.size and not preset.css:
            notes.append(f"resolutions differ ({a.size} and {b.size} px); compared in CSS pixels")
        a, b = (image.resize(size, Image.Resampling.BOX) for image in (a, b))
    return a, b, a.width / size[0], top, size, notes


def _largest_channel(a: Image.Image, b: Image.Image) -> Image.Image:
    channels = ImageChops.difference(a, b).split()
    largest = channels[0]
    for channel in channels[1:]:
        largest = ImageChops.lighter(largest, channel)
    return largest


def compare(expected: Shot, actual: Shot, preset: Preset, overlay: Path | None = None) -> Diff:
    """Compares `actual` with `expected` in the part of the document both show, outside the masks of either.

    `overlay` gets that part of `actual`, faded, with the differing pixels in red and the regions outlined in blue.
    """
    a, b, scale, top, size, notes = _prepare(expected, actual, preset)
    a, b = (_masked(image, (expected, actual), top, scale) for image in (a, b))
    offset = round(top - actual.scroll, 1)
    largest = _largest_channel(a, b)
    mask = largest.point(lambda value: 255 if value > preset.tolerance else 0)
    tile = max(1, round(preset.tile * scale))
    columns, rows = -(-mask.width // tile), -(-mask.height // tile)
    padded = Image.new("L", (columns * tile, rows * tile), 0)
    padded.paste(mask, (0, 0))
    shares = padded.resize((columns, rows), Image.Resampling.BOX).tobytes()
    counts = {}
    for index, share in enumerate(shares):
        if not share:
            continue
        x, y = index % columns, index // columns
        area = (min(tile, mask.width - x * tile)) * (min(tile, mask.height - y * tile))
        count = round(share / 255 * tile * tile)
        if count > preset.tile_share * area:
            counts[(x, y)] = count
    regions = [_region(group, largest, mask, tile, scale, offset) for group in _groups(counts)]
    regions.sort(key=lambda region: region.differing, reverse=True)
    if overlay is not None:
        _write_overlay(b, mask, regions, scale, offset, overlay)
    return Diff(
        size=size,
        top=top,
        offset=offset,
        scale=round(scale, 3),
        tiles=columns * rows,
        differing_tiles=len(counts),
        differing=mask.histogram()[255],
        max_difference=largest.getextrema()[1],
        regions=regions,
        preset=preset,
        notes=notes,
    )


def _groups(tiles: dict[tuple[int, int], int]) -> list[list[tuple[int, int]]]:
    """The tiles in groups of neighbours, diagonals included."""
    remaining = set(tiles)
    groups = []
    while remaining:
        stack = [remaining.pop()]
        group = []
        while stack:
            x, y = stack.pop()
            group.append((x, y))
            for neighbour in ((x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    stack.append(neighbour)
        groups.append(group)
    return groups


def _region(
    group: list[tuple[int, int]], largest: Image.Image, mask: Image.Image, tile: int, scale: float, offset: float
) -> Region:
    left = min(x for x, _ in group) * tile
    top = min(y for _, y in group) * tile
    right = min((max(x for x, _ in group) + 1) * tile, mask.width)
    bottom = min((max(y for _, y in group) + 1) * tile, mask.height)
    area = largest.crop((left, top, right, bottom))
    differing = mask.crop((left, top, right, bottom)).histogram()[255]
    histogram = area.histogram()
    total = sum(histogram)
    mean = sum(value * count for value, count in enumerate(histogram)) / total if total else 0.0
    return Region(
        box=(
            round(left / scale, 1),
            round(top / scale + offset, 1),
            round(right / scale, 1),
            round(bottom / scale + offset, 1),
        ),
        tiles=len(group),
        differing=differing,
        mean_difference=round(mean, 2),
        max_difference=area.getextrema()[1],
    )


def _write_overlay(
    actual: Image.Image, mask: Image.Image, regions: list[Region], scale: float, offset: float, dest: Path
) -> None:
    faded = Image.blend(actual, Image.new("RGB", actual.size, "white"), 0.7)
    faded.paste(Image.new("RGB", actual.size, (255, 0, 0)), mask=mask)
    draw = ImageDraw.Draw(faded)
    for region in regions:
        left, top, right, bottom = region.box
        box = (left * scale, (top - offset) * scale, right * scale - 1, (bottom - offset) * scale - 1)
        draw.rectangle(box, outline=(0, 0, 255), width=max(1, round(scale)))
    faded.save(dest)


def side_by_side(images: Sequence[tuple[str, Image.Image]], dest: Path, height: int | None = None) -> Path:
    """Puts `images`, (label, image) pairs, next to each other under their labels, scaled to `height`."""
    label = 16
    scaled = []
    for name, image in images:
        target = height or image.height
        width = max(1, round(image.width * target / image.height))
        resample = Image.Resampling.NEAREST if target > image.height else Image.Resampling.BOX
        scaled.append((name, image.convert("RGB").resize((width, target), resample)))
    sheet = Image.new("RGB", (sum(i.width for _, i in scaled) + 6 * (len(scaled) - 1), scaled[0][1].height + label))
    sheet.paste((255, 255, 255), (0, 0, sheet.width, sheet.height))
    draw = ImageDraw.Draw(sheet)
    x = 0
    for name, image in scaled:
        sheet.paste(image, (x, label))
        draw.text((x + 2, 2), name, fill="black")
        x += image.width + 6
    sheet.save(dest)
    return dest


def stacked(images: Sequence[tuple[str, Image.Image]], dest: Path) -> Path:
    """Puts `images`, (label, image) pairs of the same size, below each other under their labels, enlarged so that
    differences between them are easy to see: up to CROP_ZOOM times, and to at most CROP_WIDTH wide."""
    width, height = images[0][1].size
    zoom = max(1, min(CROP_ZOOM, CROP_WIDTH // max(1, width)))
    label = 16
    sheet = Image.new("RGB", (width * zoom, len(images) * (height * zoom + label)), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (name, image) in enumerate(images):
        y = index * (height * zoom + label)
        draw.text((2, y + 2), f"{name} ({zoom}x)", fill="black")
        sheet.paste(
            image.convert("RGB").resize((width * zoom, height * zoom), Image.Resampling.LANCZOS), (0, y + label)
        )
    sheet.save(dest)
    return dest


def write_review_images(
    expected: Shot, actual: Shot, diff: Diff, overlay: Path, directory: Path, name: str, labels: tuple[str, str]
) -> dict[str, str]:
    """Writes the compared part of each capture, at its own resolution, to `<name>-expected.png` and
    `<name>-actual.png`, `<name>-compare.png` (expected | actual | differing) and a crop of each of the largest regions
    to `regions/<name>-<n>.png` (expected above actual above the differing pixels), whose paths go into the regions.
    Returns the names of the images by what they show, the overlay of differing pixels included."""
    common = diff.size
    a, b = expected.load(diff.top, common), actual.load(diff.top, common)
    a.save(directory / f"{name}-expected.png")
    b.save(directory / f"{name}-actual.png")
    with Image.open(overlay) as image:
        marked = image.convert("RGB")
    a, b = (image.resize(marked.size, Image.Resampling.BOX) if image.size != marked.size else image for image in (a, b))
    compare_path = side_by_side(
        [(labels[0], a), (labels[1], b), ("differing", marked)], directory / f"{name}-compare.png"
    )
    crops = directory / "regions"
    for index, region in enumerate(diff.regions[:MAX_CROPS]):
        left, top, right, bottom = region.box
        top, bottom = top - diff.offset, bottom - diff.offset
        box = tuple(
            round(value * diff.scale)
            for value in (
                max(0, left - CROP_MARGIN),
                max(0, top - CROP_MARGIN),
                min(common[0], right + CROP_MARGIN),
                min(common[1], bottom + CROP_MARGIN),
            )
        )
        parts = [(labels[0], a.crop(box)), (labels[1], b.crop(box)), ("differing pixels", marked.crop(box))]
        crops.mkdir(exist_ok=True)
        path = stacked(parts, crops / f"{name}-{index}.png")
        region.crop = str(path.relative_to(directory))
    return {
        "compare": compare_path.name,
        "expected": f"{name}-expected.png",
        "actual": f"{name}-actual.png",
        "differing": overlay.name,
    }


def _mean_difference(a: Image.Image, b: Image.Image) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def _shifted(image: Image.Image, box: tuple[int, int, int, int], dx: int, dy: int) -> Image.Image:
    return image.crop((box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))


def _best_offset(a: Image.Image, b: Image.Image, search: int, steps: int = 4) -> tuple[float, float, float, float]:
    """The offset, in pixels, that best aligns `b` onto `a`: whole pixels up to `search`, then 1/`steps` pixels around
    the best. Returns (dx, dy, mean difference there, mean difference unshifted)."""
    inner = (search + 1, search + 1, a.width - search - 1, a.height - search - 1)
    if inner[2] - inner[0] < 4 or inner[3] - inner[1] < 4:
        unshifted = _mean_difference(a, b)
        return 0.0, 0.0, unshifted, unshifted
    reference = a.crop(inner)
    unshifted = _mean_difference(reference, b.crop(inner))
    best = (unshifted, 0, 0)
    for dy in range(-search, search + 1):
        for dx in range(-search, search + 1):
            difference = _mean_difference(reference, _shifted(b, inner, dx, dy))
            if difference < best[0] - 1e-9:
                best = (difference, dx, dy)
    size = (a.width * steps, a.height * steps)
    big_a, big_b = (image.resize(size, Image.Resampling.BICUBIC) for image in (a, b))
    big_inner = (inner[0] * steps, inner[1] * steps, inner[2] * steps, inner[3] * steps)
    reference = big_a.crop(big_inner)
    fine = (float("inf"), 0, 0)
    for fy in range(-steps + 1, steps):
        for fx in range(-steps + 1, steps):
            dx, dy = best[1] * steps + fx, best[2] * steps + fy
            difference = _mean_difference(reference, _shifted(big_b, big_inner, dx, dy))
            if difference < fine[0]:
                fine = (difference, dx, dy)
    return fine[1] / steps, fine[2] / steps, fine[0], unshifted


def _ink(gray: Image.Image, radius: int) -> tuple[int, int]:
    """How much the image departs from its background, the median over a window wider than strokes, which keeps
    gradients but not text, and the horizontal extent of the ink in pixels, between the 2nd and the 98th percentile
    of its columns, so that stray specks of a textured background don't count."""
    contrast = ImageChops.difference(gray, gray.filter(ImageFilter.MedianFilter(2 * radius + 1)))
    energy = sum(value * count for value, count in enumerate(contrast.histogram()))
    mask = contrast.point(lambda value: 255 if value > INK_CONTRAST else 0)
    columns = list(mask.resize((mask.width, 1), Image.Resampling.BOX).tobytes())
    total = sum(columns)
    if not total:
        return energy, 0
    running, low, high = 0, None, 0
    for x, value in enumerate(columns):
        running += value
        if low is None and running >= 0.02 * total:
            low = x
        if running <= 0.98 * total:
            high = x
    return energy, max(0, high - (low or 0))


def _pieces_aligned(a: Image.Image, b: Image.Image, scale: float) -> float | None:
    """The share of vertical strips of `b` that match `a` at some small offset of their own. Each strip searches
    around the previous strip's offset, so that the growing offsets of spaced-out text are found; text in another
    typeface matches nowhere. None if no strip differs."""
    strip, window = round(STRIP * scale), round(STRIP_SEARCH * scale)
    if a.width < strip or a.height < 4:
        return None
    pad = round(MAX_OFFSET * 2 * scale)
    wide_b = Image.new("L", (b.width + 2 * pad, b.height + 4), 255)
    wide_b.paste(b, (pad, 2))
    differing = aligned = 0
    previous = 0
    for x in range(0, a.width - strip + 1, strip):
        piece = a.crop((x, 0, x + strip, a.height))
        if _mean_difference(piece, wide_b.crop((x + pad, 2, x + pad + strip, a.height + 2))) <= 1:
            continue
        differing += 1
        candidates = [
            (_mean_difference(piece, wide_b.crop((x + pad + dx, 2 + dy, x + pad + dx + strip, a.height + 2 + dy))), dx)
            for dx in range(previous - window, previous + window + 1)
            if -pad <= dx <= pad
            for dy in (-1, 0, 1)
        ]
        best, dx = min(candidates)
        if best <= 4:
            aligned += 1
            previous = dx
    return round(aligned / differing, 2) if differing else None


def _on_shared_edges(a: Image.Image, b: Image.Image) -> float | None:
    """The share of the pixels that differ by more than a few levels that lie on, or next to, an edge both images
    have. None if none differ."""
    differing = ImageChops.difference(a, b).point(lambda value: 255 if value > 4 else 0)
    edges = [
        image.filter(ImageFilter.FIND_EDGES)
        .point(lambda value: 255 if value > EDGE_CONTRAST else 0)
        .filter(ImageFilter.MaxFilter(3))
        for image in (a, b)
    ]
    total = differing.histogram()[255]
    shared = ImageChops.multiply(differing, ImageChops.multiply(*edges)).histogram()[255]
    return round(shared / total, 2) if total else None


def _hue_masks(image: Image.Image) -> list[Image.Image]:
    """Masks of the coloured pixels of `image`, one per hue bin."""
    r, g, b = image.split()
    high = ImageChops.lighter(ImageChops.lighter(r, g), b)
    low = ImageChops.darker(ImageChops.darker(r, g), b)
    coloured = ImageChops.multiply(
        ImageChops.subtract(high, low).point(lambda value: 255 if value >= COLOUR_CHROMA else 0),
        high.point(lambda value: 255 if value >= 64 else 0),
    )
    hue = image.convert("HSV").getchannel("H")
    bins = len(HUES)
    return [
        ImageChops.multiply(coloured, hue.point(lambda h, k=k: 255 if h * bins // 256 == k else 0)) for k in range(bins)
    ]


def colour_name(rgb: Sequence[float]) -> str:
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb[:3]))
    if v < 0.25:
        return "black"
    if s < 0.2:
        return "white" if v > 0.85 else "grey"
    return HUES[int(h * len(HUES) + 0.5) % len(HUES)]


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{round(c):02x}" for c in rgb[:3])


def _colours_lost(a: Image.Image, b: Image.Image, scale: float) -> list[tuple[int, str, str]]:
    """The coloured pixels of `a` that `b` has no similar hue for nearby, where `b` shows another colour than the
    background around them in `a`: (pixels, colour in `a`, colour in `b`) per hue."""
    masks_a, masks_b = _hue_masks(a), _hue_masks(b)
    bins = len(HUES)
    reach = 2 * max(1, round(COLOUR_REACH * scale)) + 1
    floor = COLOUR_AREA * scale * scale
    found = []
    for k, mask in enumerate(masks_a):
        if mask.histogram()[255] < floor:
            continue
        similar = ImageChops.lighter(ImageChops.lighter(masks_b[k - 1], masks_b[k]), masks_b[(k + 1) % bins])
        gone = ImageChops.multiply(mask, ImageChops.invert(similar.filter(ImageFilter.MaxFilter(reach))))
        count = gone.histogram()[255]
        if count < floor:
            continue
        was, now = ImageStat.Stat(a, gone).mean, ImageStat.Stat(b, gone).mean
        around = ImageChops.subtract(ImageChops.subtract(gone.filter(ImageFilter.MaxFilter(reach)), gone), mask)
        background = ImageStat.Stat(a, around).median if around.getbbox() else ImageStat.Stat(a).median
        apart = max(abs(n - g) for n, g in zip(now, background, strict=True))
        if colour_name(was) != colour_name(now) and apart > 48:
            found.append((count, f"{colour_name(was)} {_hex(was)}", f"{colour_name(now)} {_hex(now)}"))
    return found


def colours_changed(a: Image.Image, b: Image.Image, scale: float) -> list[tuple[str, str, float]]:
    """Where `b` draws other colours than `a`: (colour in `a`, colour in `b`, CSS px²), the largest first."""
    changes = [(n, was, now) for n, was, now in _colours_lost(a, b, scale)]
    changes += [(n, was, now) for n, now, was in _colours_lost(b, a, scale)]
    changes.sort(reverse=True)
    return [(was, now, round(n / scale / scale)) for n, was, now in changes[:2]]


def measure(expected: Shot, actual: Shot, diff: Diff, region: Region) -> Measurement:
    """Measures how `actual` differs from `expected` in `region`, with a margin around it."""
    a, b = expected.load(diff.top, diff.size), actual.load(diff.top, diff.size)
    if a.size != b.size:
        a, b = (image.resize(diff.size, Image.Resampling.BOX) for image in (a, b))
    scale = a.width / diff.size[0]
    left, top, right, bottom = region.box
    margin = 4
    box = (
        max(0, round((left - margin) * scale)),
        max(0, round((top - diff.offset - margin) * scale)),
        min(a.width, round((right + margin) * scale)),
        min(a.height, round((bottom - diff.offset + margin) * scale)),
    )
    gray_a, gray_b = a.crop(box).convert("L"), b.crop(box).convert("L")
    dx, dy, residual, unshifted = _best_offset(gray_a, gray_b, round(MAX_OFFSET * scale))
    radius = round(BACKGROUND_RADIUS * scale)
    (ink_a, extent_a), (ink_b, extent_b) = _ink(gray_a, radius), _ink(gray_b, radius)
    return Measurement(
        (round(dx / scale, 2), round(dy / scale, 2)),
        round(residual, 1),
        round(unshifted, 1),
        round(ink_b / ink_a, 2) if ink_a else None,
        (round(extent_a / scale, 1), round(extent_b / scale, 1)),
        _pieces_aligned(gray_a, gray_b, scale),
        _on_shared_edges(gray_a, gray_b),
        tuple(colours_changed(a.crop(box), b.crop(box), scale)),
    )


@dataclass(frozen=True)
class Measurement:
    """How a region differs: `offset`, in CSS px, best aligns the actual rendering onto the expected one, with mean
    luminance differences (0-255) there and unshifted; `ink` is the actual rendering's ink relative to the expected
    one's (below 1: thinner or lighter strokes, or fewer marks), `ink_width` the width of the ink in each, in CSS px,
    `pieces_aligned` the share of differing strips that match on their own (spacing changes all, glyphs none),
    `on_edges` the share of differing pixels on edges both renderings have (anti-aliasing: most) and `colours` the
    colours the actual rendering draws instead of the expected one's (see `colours_changed`)."""

    offset: tuple[float, float]
    difference_aligned: float
    difference_unshifted: float
    ink: float | None
    ink_width: tuple[float, float]
    pieces_aligned: float | None = None
    on_edges: float | None = None
    colours: tuple[tuple[str, str, float], ...] = ()

    def reading(self, text: bool = True) -> str:
        """What the measurements mean, in words, for an agent that can't see the crops. Without `text` in the region,
        the width of the ink, e.g. of a textured background, says nothing about spacing or glyphs, and is left out."""
        dx, dy = self.offset
        aligned, unshifted, ink = self.difference_aligned, self.difference_unshifted, self.ink
        before, after = self.ink_width
        parts = []
        moved = bool(dx or dy) and aligned <= 8 and aligned <= 0.5 * unshifted
        if moved:
            parts.append(f"moved by ({dx:+g}, {dy:+g}) CSS px, otherwise nearly the same")
        elif unshifted < 3 and (self.on_edges is None or self.on_edges >= 0.5):
            parts.append("only edge pixels differ: anti-aliasing or a sub-pixel position")
        elif unshifted < 3:
            parts.append("faint differences away from shared edges: a texture, shading or background image changed")
        changed_ink = ink is not None and abs(ink - 1) >= 0.08
        if ink is not None and changed_ink:
            if ink <= 0.5:
                strokes = "content missing, or much fainter"
            elif ink >= 2:
                strokes = "content added, or much bolder"
            elif ink < 1:
                strokes = "thinner or lighter strokes, or fewer marks"
            else:
                strokes = "thicker or darker strokes, or more marks"
            parts.append(f"{abs(ink - 1):.0%} {'less' if ink < 1 else 'more'} ink: {strokes}")
        # Other glyphs or spacing change the width of text by a fraction; more is content that came or went, which
        # "shapes differ" covers, or a background whose visible part changed.
        change = abs(after - before)
        widened = text and not moved and 1.5 <= change <= 0.3 * max(before, after)
        # A typeface of about the same weight changes the width too; only spaced-out glyphs match piece by piece.
        pieces = self.pieces_aligned
        spacing = widened and not changed_ink and pieces is not None and pieces >= 0.6
        if widened:
            same = " with about the same ink: letter spacing or glyph widths" if spacing else ""
            parts.append(f"content {abs(after - before):g} CSS px {'wider' if after > before else 'narrower'}{same}")
        # Spaced-out text can't be aligned as a whole either, so this would point at the wrong cause.
        if not moved and not spacing and unshifted >= 3 and aligned > 8:
            parts.append("shapes differ even at the best alignment: glyphs, typeface or content")
        for was, now, area in self.colours:
            parts.append(f"colour changed: {was} drawn {now} ({area:g} CSS px²)")
        return "; ".join(parts) or "small differences without a clear pattern"


def reference_hint(previous: Shot, current: Shot, reference: Shot, regions: Sequence[Region]) -> None:
    """Notes in each region whether `current` is closer to `reference`, another engine's rendering, than `previous`.

    Regions are in `current`'s viewport. The engines rasterize differently, so this is a hint for triage: a change
    toward the reference is more likely an improvement, one away from it more likely a regression.
    """
    shots = (previous, current, reference)
    images = [shot.load(shot.scroll, shot.size).resize(shot.size, Image.Resampling.BOX) for shot in shots]
    for region in regions:
        left, top, right, bottom = region.box
        boxes = [
            (
                round(left),
                round(top + current.scroll - shot.scroll),
                round(right),
                round(bottom + current.scroll - shot.scroll),
            )
            for shot in shots
        ]
        if any(
            box[0] < 0 or box[1] < 0 or box[2] > shot.size[0] or box[3] > shot.size[1]
            for box, shot in zip(boxes, shots, strict=True)
        ):
            region.reference = "not all in the reference's or the earlier capture's viewport"
            continue
        before, after, ref = (image.crop(box) for image, box in zip(images, boxes, strict=True))
        old, new = _mean(_largest_channel(before, ref)), _mean(_largest_channel(after, ref))
        if abs(new - old) < max(1.0, 0.1 * max(old, new)):
            region.reference = f"as far from the reference as before ({old:.1f} -> {new:.1f})"
        elif new < old:
            region.reference = f"closer to the reference ({old:.1f} -> {new:.1f})"
        else:
            region.reference = f"further from the reference ({old:.1f} -> {new:.1f})"


def _mean(image: Image.Image) -> float:
    histogram = image.histogram()
    total = sum(histogram)
    return sum(value * count for value, count in enumerate(histogram)) / total if total else 0.0


# Computed style properties of DOM snapshot elements that a rendering difference often comes from.
STYLE_KEYS = ("font_family", "font_size", "font_weight", "font_style", "color")
# Properties that change how an element is painted, recorded only when not at their initial value (snapshots of
# version 2 on).
PAINT_KEYS = ("background_clip", "background_image", "clip_path", "mask", "filter", "opacity")


def elements_in(elements: Sequence[dict[str, object]], box: CssBox, limit: int = 5) -> list[dict[str, object]]:
    """The elements of a DOM snapshot whose boxes best match `box`, by intersection over union, in the snapshot's
    coordinates: the element a region is about, rather than whatever small marks it also touches."""
    left, top, right, bottom = box
    area = (right - left) * (bottom - top)
    scored = []
    for element in elements:
        x, y, width, height = element["rect"]  # type: ignore[misc]
        overlap = (min(right, x + width) - max(left, x)) * (min(bottom, y + height) - max(top, y))
        if min(right, x + width) > max(left, x) and min(bottom, y + height) > max(top, y):
            scored.append((overlap / (area + width * height - overlap), element))
    scored.sort(key=lambda entry: entry[0], reverse=True)
    return [_short(element) for _, element in scored[:limit]]


def _short(element: dict[str, object]) -> dict[str, object]:
    keys = ("path", "text", "rect", *STYLE_KEYS, *PAINT_KEYS, "image", "src")
    return {key: element[key] for key in keys if element.get(key) not in (None, "")}


def _comparable(key: str, value: object) -> object:
    """`value` as engines agree on it: Chromium quotes font family names, Servo doesn't."""
    if key == "font_family" and isinstance(value, str):
        return ",".join(name.strip().strip("\"'") for name in value.split(","))
    return value


def dom_changes(
    before: Sequence[dict[str, object]], after: Sequence[dict[str, object]], limit: int = 40, paint: bool = True
) -> list[dict[str, object]]:
    """Elements whose text, box, style or image load state differs between two DOM snapshots, matched by path, text,
    style and image changes first. `paint` compares PAINT_KEYS too, for snapshots that both record them."""
    earlier = {str(e["path"]): e for e in before}
    keys = ("text", *STYLE_KEYS, *(PAINT_KEYS if paint else ()))
    changes = []
    for element in after:
        old = earlier.get(str(element["path"]))
        if old is None:
            continue
        style = {
            key: [old.get(key), element.get(key)]
            for key in keys
            if _comparable(key, old.get(key)) != _comparable(key, element.get(key))
        }
        image = old.get("image") is not None and old.get("image") != element.get("image")
        moved = max(abs(a - b) for a, b in zip(old["rect"], element["rect"], strict=True))  # type: ignore[call-overload]
        if style or image or moved > 0.5:
            change: dict[str, object] = {"path": element["path"], "text": element.get("text", "")}
            if style:
                change["style"] = style
            if image:
                change["image"] = [old.get("image"), element.get("image")]
                change["src"] = element.get("src")
            if moved > 0.5:
                change["rect"] = [old["rect"], element["rect"]]
            changes.append((not (style or image), -moved, change))
    changes.sort(key=lambda entry: entry[:2])
    paths = {str(e["path"]) for e in after}
    result = [change for _, _, change in changes[:limit]]
    missing = [str(e["path"]) for e in before if str(e["path"]) not in paths]
    added = [str(e["path"]) for e in after if str(e["path"]) not in earlier]
    if missing:
        result.append({"missing": missing[:20], "count": len(missing)})
    if added:
        result.append({"added": added[:20], "count": len(added)})
    return result


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
