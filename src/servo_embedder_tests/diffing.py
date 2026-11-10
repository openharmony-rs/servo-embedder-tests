"""Comparing two captures of the same page: where they differ, how much, and what is there.

Captures are screenshots of a viewport with a known size in CSS pixels. Pixels differ when a channel differs by more
than a tolerance; the image is split into tiles, and a tile differs when more than a share of its pixels differ. A
count over the whole image would dilute a small but real change, such as one heading drawn in a fallback font on a
long page. Neighbouring differing tiles form regions, which are reported in CSS pixels with what the DOM snapshots
have there.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

# (left, top, right, bottom) in CSS pixels of the viewport.
CssBox = tuple[float, float, float, float]
# Regions that get a crop for review, the largest first.
MAX_CROPS = 8
# CSS pixels of context around a region in its crop.
CROP_MARGIN = 8


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
# of 576 px) over 80 captures of GenUI pages: the largest tile differed in 12 pixels (a 1 px image edge), while one
# element in another font family, with letter-spacing 0.3px or moved by 1 px gave tiles of 131 pixels or more.
BASELINE = Preset(tolerance=24, tile=16, tile_share=0.05, css=False)
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


def write_review_images(
    expected: Shot, actual: Shot, diff: Diff, overlay: Path, directory: Path, name: str, labels: tuple[str, str]
) -> Path:
    """Writes `<name>-compare.png` (expected | actual | differing) and a crop of each of the largest regions to
    `regions/<name>-<n>.png`, whose paths go into the regions. Returns the compare image."""
    common = diff.size
    a, b = expected.load(diff.top, common), actual.load(diff.top, common)
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
        parts = [(labels[0], a.crop(box)), (labels[1], b.crop(box)), ("differing", marked.crop(box))]
        height = parts[0][1].height
        # Small regions are enlarged so that glyphs stay recognizable; large ones are kept below 512 px wide each.
        target = max(height, 160)
        target = min(target, 512 * height // max(1, parts[0][1].width))
        crops.mkdir(exist_ok=True)
        path = side_by_side(parts, crops / f"{name}-{index}.png", max(target, 16))
        region.crop = str(path.relative_to(directory))
    return compare_path


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


def elements_in(elements: Sequence[dict[str, object]], box: CssBox, limit: int = 5) -> list[dict[str, object]]:
    """The smallest elements of a DOM snapshot that overlap `box`, in the snapshot's coordinates."""
    left, top, right, bottom = box
    found = []
    for element in elements:
        x, y, width, height = element["rect"]  # type: ignore[misc]
        if x < right and x + width > left and y < bottom and y + height > top:
            found.append(element)
    found.sort(key=lambda e: e["rect"][2] * e["rect"][3])  # type: ignore[index]
    return [_short(element) for element in found[:limit]]


def _short(element: dict[str, object]) -> dict[str, object]:
    return {key: element[key] for key in ("path", "text", "rect", *STYLE_KEYS) if element.get(key) not in (None, "")}


def dom_changes(
    before: Sequence[dict[str, object]], after: Sequence[dict[str, object]], limit: int = 40
) -> list[dict[str, object]]:
    """Elements whose box or style differs between two DOM snapshots, matched by path, style changes first."""
    earlier = {str(e["path"]): e for e in before}
    changes = []
    for element in after:
        old = earlier.get(str(element["path"]))
        if old is None:
            continue
        style = {key: [old.get(key), element.get(key)] for key in STYLE_KEYS if old.get(key) != element.get(key)}
        moved = max(abs(a - b) for a, b in zip(old["rect"], element["rect"], strict=True))  # type: ignore[call-overload]
        if style or moved > 0.5:
            change: dict[str, object] = {"path": element["path"], "text": element.get("text", "")}
            if style:
                change["style"] = style
            if moved > 0.5:
                change["rect"] = [old["rect"], element["rect"]]
            changes.append((not style, -moved, change))
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
