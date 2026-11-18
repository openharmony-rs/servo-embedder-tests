"""The page scenario report: `report.html` in a run's artifacts folder, for people to see what changed in each page's
rendering and whether that is for the better.

A run with baselines and a reference engine is a three-way comparison. The regression check compares the engine's new
rendering of a page with its old one, the baseline: a page that renders as before needs nothing more. Where it
changed, the reference engine's rendering, such as ArkWeb's, judges each changed region (`toward_reference` in
`diff.json`): a change toward it is likely an improvement, one away from it likely a regression. Per page, the report
gives that verdict first, then each changed capture as old | new | reference | changed pixels, with the regions
numbered, cropped side by side and measured. The engine comparison, the new rendering against the reference's, shows
the gaps that remain; it is folded away where there is a regression check. Without baselines, the engine comparison
is all there is.

Where an agent triaged the run, the report adds the image model's answers (`vision.json`, from the compare-renderings
skill) and the agent's conclusion (`triage.json`), so that people can check them against the pictures:

    {"agent": "<who>", "verdict": "regression", "summary": "<text>", "regions": {"<capture>-<n>": "<text>"}}

`<capture>-<n>` names a region as its crop does (`regions/<capture>-<n>.png`).

The report shows the images in the artifacts folder; `report-standalone.html`, written on request, is the same report
with its images inlined, one file to share.
"""

from __future__ import annotations

import base64
import datetime
import functools
import io
import json
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

from PIL import Image, features

from .expectations import Result, RunReport
from .plugin import TEST_ARTIFACTS_MARKER

REPORT = "report.html"
# The report as one file to share, with its images inlined, in WebP of this quality (see `Images`).
STANDALONE = "report-standalone.html"
INLINE_QUALITY = 80
TRIAGE = "triage.json"
VISION = "vision.json"
BASELINE = "test_matches_baseline"
REFERENCE = "test_matches_reference"
RENDERS = "test_renders"
TESTS = {RENDERS: "renders", BASELINE: "baseline", REFERENCE: "reference"}
KEY = re.compile(r"^page_scenarios/test_pages\.py::(test_\w+)\[(.+)\]$")
SERVO = re.compile(r"\bServo/(\S+)")
# A page's verdict: its title and CSS class, most urgent first, which is the order of the pages in the report.
VERDICTS = {
    "error": "Not compared",
    "regression": "Likely regression",
    "mixed": "Mixed",
    "unclear": "Changed, unclear",
    "unjudged": "Changed, not judged",
    "improvement": "Likely improvement",
    "new": "No baseline",
    "stored": "Baseline stored",
    "unchanged": "Unchanged",
}
# Region crops are enlarged up to this factor, to at most this many CSS px; strips wider than WIDE times their height
# go one per row, up to twice as wide.
CROP_ZOOM = 4
CROP_SIZE = 340
WIDE = 2.5
CROP_MARGIN = 8
# The distances to the reference in a region's `reference`.
DISTANCES = re.compile(r"\(([\d.]+) -> ([\d.]+)\)$")
# The share of a page's changed pixels whose regions must move toward the reference, or away from it, for its verdict
# to say so; a few pixels that moved shouldn't make a page an improvement.
VERDICT_SHARE = 0.1

STYLE = """
:root {
  --bg: #ffffff; --fg: #1d1d1f; --muted: #6b6b70; --line: #dcdce0; --card: #f5f5f7; --pass: #1a7f37;
  --fail: #c62828; --skip: #75757a; --box: #1565c0; --note: #fff7d6;
  --better: #1a7f37; --worse: #c62828; --mixed: #a35f00; --neutral: #6b6b70;
  --old: #8a8a8f; --new: #1565c0; --ref: #7b4fc9; --changed: #d32f2f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --fg: #e8e8ea; --muted: #9a9aa0; --line: #34353b; --card: #1f2024; --pass: #57b75b;
    --fail: #ef5350; --skip: #9a9aa0; --box: #64b5f6; --note: #2c2814;
    --better: #57b75b; --worse: #ef5350; --mixed: #e0a040; --neutral: #9a9aa0;
    --old: #9a9aa0; --new: #64b5f6; --ref: #b39ddb; --changed: #ef5350;
  }
}
body { margin: 0 auto; padding: 16px; max-width: 1600px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, sans-serif; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 32px 0 6px; padding-top: 12px; border-top: 2px solid var(--line); }
h3 { font-size: 16px; margin: 20px 0 6px; }
h4 { font-size: 14px; margin: 18px 0 8px; }
code { font-size: 12px; }
a { color: inherit; }
.muted { color: var(--muted); }
.status { font-weight: 600; }
.PASS { color: var(--pass); } .FAIL, .TIMEOUT, .CRASH { color: var(--fail); } .SKIP { color: var(--skip); }
.unexpected::after { content: " (unexpected)"; font-weight: 400; }
table { border-collapse: collapse; margin: 8px 0; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
.wide { overflow-x: auto; }
.legend { background: var(--card); border-radius: 6px; padding: 10px 12px; margin: 12px 0; }
.legend p { margin: 0 0 8px; }
dl { margin: 6px 0 0; display: grid; grid-template-columns: max-content 1fr; gap: 2px 10px; }
dt { color: var(--muted); }
dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.legend dt { color: var(--fg); font-weight: 600; }
.side::before { content: ""; display: inline-block; width: 9px; height: 9px; margin-right: 5px; border-radius: 2px;
  background: var(--side, var(--muted)); vertical-align: 0; }
.side.old { --side: var(--old); } .side.new { --side: var(--new); } .side.ref { --side: var(--ref); }
.side.changed { --side: var(--changed); }
.verdict { display: inline-block; padding: 3px 8px; border-radius: 4px; font: 600 12px/1.3 system-ui, sans-serif;
  border: 1px solid transparent; vertical-align: 2px; white-space: nowrap; }
.v-regression { background: var(--worse); color: #fff; } .v-improvement { background: var(--better); color: #fff; }
.v-mixed { background: var(--mixed); color: #fff; } .v-unclear { background: var(--neutral); color: #fff; }
.v-unjudged, .v-new { border-color: var(--neutral); color: var(--fg); }
.v-error { border-color: var(--fail); color: var(--fail); }
.v-unchanged { border-color: var(--line); color: var(--muted); }
.tally { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.why { margin: 4px 0 8px; }
.toward { display: inline-block; padding: 1px 6px; border-radius: 3px; font-weight: 600; font-size: 12px;
  border: 1px solid transparent; }
.t-closer { background: var(--better); color: #fff; } .t-further { background: var(--worse); color: #fff; }
.t-same { background: var(--neutral); color: #fff; } .t-none { border-color: var(--neutral); color: var(--muted); }
.shots { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
  align-items: start; }
figure { margin: 0; min-width: 0; max-width: 100%; }
figcaption { font-size: 12px; margin-top: 3px; }
.shot { position: relative; line-height: 0; border: 1px solid var(--line); max-width: 100%; }
.shot img { width: 100%; height: auto; }
.flip .top { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
.flip:hover .top { visibility: hidden; }
.box { position: absolute; border: 2px solid var(--box); box-sizing: border-box; }
.box span { position: absolute; top: -2px; left: -2px; padding: 2px 4px; background: var(--box); color: #fff;
  font: 600 11px/1 system-ui, sans-serif; }
#boxes:not(:checked) ~ main .box { display: none; }
.regions { list-style: none; padding: 0; display: grid; gap: 12px; }
.regions > li { background: var(--card); border-radius: 6px; padding: 8px 10px; min-width: 0; }
.region-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }
.crops { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 4px; align-items: flex-start; }
.crops.stacked { flex-direction: column; }
.view { position: relative; overflow: hidden; max-width: 100%; border: 1px solid var(--line); background: #808080; }
.view img { position: absolute; max-width: none; height: auto; }
.badge { display: inline-block; min-width: 18px; padding: 1px 5px; background: var(--box); color: #fff;
  border-radius: 3px; text-align: center; font-weight: 600; }
.note { background: var(--note); border-radius: 6px; padding: 8px 10px; margin: 8px 0; }
.note p { margin: 4px 0; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--card); padding: 8px; border-radius: 6px; }
details { margin: 8px 0; } summary { cursor: pointer; }
details.gaps > summary { font-size: 16px; font-weight: 600; margin: 16px 0 6px; }
"""


@dataclass(frozen=True)
class Side:
    """One rendering in a comparison: `role` is old, new or ref, `name` what the report calls it, `detail` which build
    and when it was rendered."""

    role: str
    name: str
    detail: str

    def label(self, extra: str = "") -> str:
        return f'<span class="side {self.role}">{escape(self.name)}</span>{extra}'


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _status(result: Result | None) -> str:
    if result is None:
        return '<span class="muted">-</span>'
    unexpected = " unexpected" if result.unexpected else ""
    return f'<span class="status {escape(result.status)}{unexpected}">{escape(result.status)}</span>'


def _element(element: dict) -> str:
    if element.get("image"):
        return f"&lt;img&gt; {escape(str(element.get('src', '')))} ({escape(str(element['image']))})"
    font = " ".join(
        str(element[key]) for key in ("font_family", "font_size", "font_weight", "font_style") if element.get(key)
    )
    return f"{escape(repr(element.get('text', '')))} <span class='muted'>{escape(font)}</span>"


def _dom_change(change: dict) -> str:
    if "missing" in change or "added" in change:
        kind = "missing" if "missing" in change else "added"
        return f"{change['count']} element(s) {kind}: {escape(', '.join(change[kind][:5]))}"
    parts = [f"<code>{escape(str(change['path']))}</code> {escape(repr(change.get('text', '')))}"]
    for key, (old, new) in change.get("style", {}).items():
        parts.append(f"{escape(key)} {escape(str(old))} → {escape(str(new))}")
    if "image" in change:
        old, new = change["image"]
        parts.append(f"image {escape(str(old))} → {escape(str(new))} ({escape(str(change.get('src')))})")
    if "rect" in change:
        old, new = change["rect"]
        parts.append(f"box {escape(str(old))} → {escape(str(new))}")
    return "; ".join(parts)


def _product(info: object) -> str:
    """What a rendering recorded about its engine, as a product name: Servo, ArkWeb, or the engine's own name; empty
    if it recorded nothing."""
    if not isinstance(info, dict):
        return ""
    if info.get("arkweb"):
        return "ArkWeb"
    if SERVO.search(str(info.get("user_agent", ""))):
        return "Servo"
    return str(info.get("engine", ""))


def _build(info: object) -> str:
    """The engine, the name --engine-version gave its build and the version it reports."""
    if not isinstance(info, dict):
        return ""
    if info.get("arkweb"):
        detected = f"ArkWeb {info['arkweb']}" + (f", Chromium {info['chromium']}" if info.get("chromium") else "")
    elif match := SERVO.search(str(info.get("user_agent", ""))):
        detected = f"Servo {match.group(1)}"
    else:
        detected = str(info.get("user_agent", ""))
    return " · ".join(part for part in (str(info.get("engine", "")), str(info.get("version") or ""), detected) if part)


def _origin(stored: object) -> tuple[object, str]:
    """The engine of stored captures a comparison compared with, and when they were stored or that they were rendered
    in the run."""
    if not isinstance(stored, dict):
        return None, ""
    if not isinstance(stored.get("engine"), dict):
        return stored, ""  # older diff.json: the baseline's engine only
    when = "rendered in this run" if stored.get("from") == "rendered in this run" else ""
    if stored.get("stored"):
        when = f"stored {str(stored['stored'])[:10]}" + (f" ({stored['reason']})" if stored.get("reason") else "")
    return stored["engine"], when


def _detail(info: object, when: str) -> str:
    return " · ".join(part for part in (_build(info), when) if part)


def _sides(test: str, report: dict) -> dict[str, Side]:
    """The renderings a comparison's `diff.json` compared, by role; for a regression check, with the reference
    engine's if it judged the changes."""
    labels = report.get("labels", ["expected", "actual"])
    actual = report.get("actual")
    product = _product(actual) or labels[1]
    new = Side("new", f"New {product}", _detail(actual, "this run"))
    expected, when = _origin(report.get("expected"))
    if test == REFERENCE:
        return {"ref": Side("ref", _product(expected) or labels[0], _detail(expected, when)), "new": new}
    # The baselines are the same engine's, which may not have recorded what it is.
    sides = {"old": Side("old", f"Old {product}", _detail(expected, when)), "new": new}
    if "reference" in report:
        engine, when = _origin(report["reference"])
        sides["ref"] = Side("ref", _product(engine) or "the reference", _detail(engine, when))
    return sides


def _toward(region: dict) -> str | None:
    """Whether a changed region moved "closer" to the reference's rendering, "further" or stayed the "same"."""
    return region.get("toward_reference")


def _toward_chip(region: dict, reference: str) -> str:
    toward = _toward(region)
    titles = {"closer": f"Closer to {reference}", "further": f"Further from {reference}", "same": "As far as before"}
    title = titles.get(toward or "", "Not judged")
    chip = f'<span class="toward t-{toward or "none"}">{escape(title)}</span>'
    if match := DISTANCES.search(str(region.get("reference") or "")):
        what = f"How far from {reference}'s rendering the old one is, and the new one"
        chip += f'<span class=muted title="{escape(what)}">{match.group(1)} → {match.group(2)}</span>'
    return chip


@dataclass
class Verdict:
    key: str
    why: str

    @property
    def chip(self) -> str:
        return f'<span class="verdict v-{self.key}">{VERDICTS[self.key]}</span>'


def _share(pixels: int, total: int) -> str:
    share = 100 * pixels / total if total else 0
    return "<1%" if 0 < share < 1 else f"{share:.0f}%"


def _judge(report: dict, sides: dict[str, Side]) -> Verdict:
    """The verdict on a regression check that found changes, from how its regions moved relative to the reference."""
    captures = report.get("captures", {})
    changed = {name: entry for name, entry in captures.items() if not entry.get("matches")}
    regions = [region for entry in changed.values() for region in entry.get("regions", [])]
    where = f"{len(regions)} changed region(s) in {len(changed)} capture(s)"
    if not regions:
        return Verdict("unclear", "; ".join(f"{name}: {entry.get('summary')}" for name, entry in changed.items()))
    if report.get("description_error"):
        return Verdict("unjudged", f"{where}; describing them failed: {report['description_error']}")
    if "ref" not in sides:
        if any(region.get("reference") for region in regions) and not any("toward_reference" in r for r in regions):
            return Verdict("unjudged", f"{where}; compared by an earlier version, which didn't judge them this way")
        return Verdict("unjudged", f"{where}; no reference rendering stored to judge them by")
    reference = sides["ref"].name
    pixels = {"closer": 0, "further": 0, "same": 0, None: 0}
    counts = dict.fromkeys(pixels, 0)
    for region in regions:
        toward = _toward(region)
        pixels[toward] += int(region.get("differing", 0))
        counts[toward] += 1
    total = sum(pixels.values())
    parts = [
        f"{counts[toward]} {words} ({_share(pixels[toward], total)} of the changed pixels)"
        for toward, words in (
            ("further", f"further from {reference}"),
            ("closer", f"closer to {reference}"),
            ("same", "as far as before"),
            (None, "not judged"),
        )
        if counts[toward]
    ]
    why = f"{where}: {', '.join(parts)}"
    further, closer = (total and pixels[toward] >= VERDICT_SHARE * total for toward in ("further", "closer"))
    if further and closer:
        return Verdict("mixed", why)
    if further:
        return Verdict("regression", why)
    if closer:
        return Verdict("improvement", why)
    if not counts["same"] and not counts["further"] and not counts["closer"]:
        reason = next((str(region["reference"]) for region in regions if region.get("reference")), "")
        return Verdict("unjudged", f"{where}; {reason or f'{reference} was not compared'}")
    return Verdict("unclear", f"{why}; too little of the change moved toward {reference} or away from it to tell")


def _verdict(baseline: Result | None, report: dict, sides: dict[str, Side]) -> Verdict | None:
    """A page's verdict from its regression check, its result and `diff.json`; None if it had none."""
    captures = report.get("captures", {})
    if captures:
        if all(entry.get("matches") for entry in captures.values()):
            return Verdict("unchanged", "Renders as before, so there is nothing to judge")
        return _judge(report, sides)
    if baseline is None or baseline.status == "SKIP":
        return None
    if baseline.status == "PASS":
        return Verdict("stored", "This run's rendering was stored as the baseline, so nothing was compared")
    if baseline.status == "FAIL" and "no baseline in" in baseline.message:
        return Verdict("new", "No baseline stored yet: only the engine comparison applies")
    message = f": {baseline.message}" if baseline.message else ""
    return Verdict("error", f"The regression check ended {baseline.status}{message}")


def _shot(images: list[tuple[str, str]], width: float, boxes: str, caption: str) -> str:
    """A capture image, or two that flip on hover, with the region boxes over it."""
    flip = len(images) > 1
    tags = "".join(
        f'<img src="{escape(src)}" alt="{escape(alt)}"{" class=top" if flip and index else ""}>'
        for index, (src, alt) in enumerate(images)
    )
    return (
        f'<figure><div class="shot{" flip" if flip else ""}" style="max-width: {width:g}px">{tags}{boxes}</div>'
        f"<figcaption>{caption}</figcaption></figure>"
    )


@functools.lru_cache(maxsize=16)
def _open(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


class Images:
    """How the report refers to the images in the artifacts folder `root`: by their paths, or, `inline`, as data URIs,
    so that the report is one file to share. Inlined, full captures have one pixel per CSS pixel and crops up to twice
    the pixels they are shown with, in WebP of INLINE_QUALITY: on 8 GenUI pages, 15 MB instead of 106 MB of PNGs."""

    def __init__(self, root: Path, inline: bool = False) -> None:
        self.root = root
        self.inline = inline
        self._uris: dict[tuple[str, int | None], str] = {}

    @staticmethod
    def _uri(image: Image.Image) -> str:
        webp = features.check("webp")
        buffer = io.BytesIO()
        image.save(buffer, "WEBP" if webp else "JPEG", quality=INLINE_QUALITY)
        kind = "webp" if webp else "jpeg"
        return f"data:image/{kind};base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"

    def src(self, path: str, width: float | None = None) -> str:
        """What an `<img src>` names for `path`, relative to `root`, an image shown at most `width` CSS px wide."""
        if not self.inline:
            return path
        key = (path, None if width is None else round(width))
        if key not in self._uris:
            image = _open(self.root / path)
            if width is not None and image.width > width:
                image = image.resize((round(width), max(1, round(width * image.height / image.width))), Image.LANCZOS)
            self._uris[key] = self._uri(image)
        return self._uris[key]

    def part(self, path: str, box: tuple[float, float, float, float], size: list, shown: float) -> str:
        """An `<img>` of the part `box`, in CSS px of the `size` the image `path` shows, `shown` CSS px wide."""
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        if not self.inline:
            style = (
                f"width: {100 * size[0] / width:.3f}%; left: {-100 * left / width:.3f}%; "
                f"top: {-100 * top / height:.3f}%"
            )
            return f'<img src="{escape(path)}" alt="" loading="lazy" style="{style}">'
        image = _open(self.root / path)
        scale = image.width / size[0]
        part = image.crop(tuple(round(value * scale) for value in box))
        if part.width > 2 * shown:
            part = part.resize((round(2 * shown), max(1, round(2 * shown * part.height / part.width))), Image.LANCZOS)
        return f'<img src="{self._uri(part)}" alt="" style="width: 100%; height: 100%; left: 0; top: 0">'

    def link(self, path: str, text: str) -> str:
        """`text` linking to the file `path`, which an inlined report doesn't have."""
        return text if self.inline else f'<a href="{escape(path)}">{text}</a>'


def _crop(
    images: Images, path: str, caption: str, box: tuple[float, float, float, float], size: list, zoom: float
) -> str:
    """The part `box` (CSS px of the compared part, `size`) of the image `path`, enlarged by `zoom`."""
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    return (
        f'<figure><div class="view" style="width: {width * zoom:.0f}px; aspect-ratio: {width:g} / {height:g}">'
        f"{images.part(path, box, size, width * zoom)}</div><figcaption>{caption}</figcaption></figure>"
    )


def _capture(
    images: Images,
    prefix: str,
    folder: Path,
    name: str,
    entry: dict,
    panels: list[tuple[Side, str]],
    answers: dict,
    notes: dict,
    judged_by: Side | None,
) -> str:
    """A differing capture: the compared images, `panels` of (side, image key) with the side that changed last, the
    differing pixels, and a card per region. `judged_by` is the reference that judged the changes, if any."""
    files = entry.get("images")
    regions = entry.get("regions", [])
    shown = [(index, region) for index, region in enumerate(regions) if region.get("crop")]
    reference = judged_by.name if judged_by else ""
    summary = str(entry.get("summary", ""))
    towards = [_toward(region) for region in regions]
    if regions:
        summary = f"{len(regions)} {'changed' if panels[0][0].role == 'old' else 'differing'} region(s)"
        words = {"further": f"further from {reference}", "closer": f"closer to {reference}", "same": "as far as before"}
        judged = [f"{towards.count(toward)} {words[toward]}" for toward in words if toward in towards]
        if judged_by is not None and judged:
            summary += ": " + ", ".join(judged)
    title = f"<h4>{escape(name)} <span class=muted>· {escape(summary)}</span></h4>"
    # Why none of the regions could be judged, such as viewports of other widths, goes here once.
    reasons = {str(region.get("reference") or "") for region in regions}
    common = reasons.pop() if judged_by is not None and regions and not any(towards) and len(reasons) == 1 else ""
    if common:
        title += f"<p class=muted>{escape(reference)}: {escape(common)}</p>"
    if not files:
        compare = entry.get("compare")
        return title + (f'<img src="{escape(images.src(f"{folder.name}/{compare}"))}" alt="">' if compare else "")
    base = f"{folder.name}/"
    width, height = entry["size"]
    offset = entry["offset"]
    panels = [(side, key) for side, key in panels if files.get(key)]
    boxes = ""
    for index, region in shown:
        left, top, right, bottom = region["box"]
        style = (
            f"left: {100 * left / width:.2f}%; top: {100 * (top - offset) / height:.2f}%; "
            f"width: {100 * (right - left) / width:.2f}%; height: {100 * (bottom - top) / height:.2f}%"
        )
        target = f"{escape(prefix)}-{escape(name)}-{index}"
        boxes += f'<a class="box" href="#{target}" style="{style}"><span>{index + 1}</span></a>'
    first, *_ = panels
    changes = "Changed pixels" if judged_by is not None or first[0].role == "old" else "Differing pixels"
    differing = Side("changed", changes, "")
    shots = []
    for side, key in panels:
        stack = [(images.src(base + files[key], width), side.name)]
        caption = side.label()
        if side.role == "new":
            stack = [(images.src(base + files[first[1]], width), first[0].name), *stack]
            caption = side.label(f" <span class=muted>(hover: {escape(first[0].name)})</span>")
        shots.append(_shot(stack, width, boxes, caption))
    differing_src = images.src(base + files["differing"], width)
    shots.append(_shot([(differing_src, changes)], width, boxes, differing.label()))
    items = []
    for index, region in shown:
        crop_id = Path(region["crop"]).stem
        left, top, right, bottom = region["box"]
        top, bottom = top - offset, bottom - offset
        area = (
            max(0, left - CROP_MARGIN),
            max(0, top - CROP_MARGIN),
            min(width, right + CROP_MARGIN),
            min(height, bottom + CROP_MARGIN),
        )
        crop_width, crop_height = area[2] - area[0], area[3] - area[1]
        stacked = crop_width > WIDE * crop_height
        zoom = min(CROP_ZOOM, (2 if stacked else 1) * CROP_SIZE / crop_width, CROP_SIZE / crop_height)
        crops = [
            _crop(images, base + files[key], side.label(), area, entry["size"], zoom)
            for side, key in [*panels, (differing, "differing")]
        ]
        facts = [("Measured", escape(str(region.get("reading") or "")))]
        if judged_by is not None and region.get("reference") and _toward(region) is None and not common:
            facts.append((reference, escape(str(region["reference"]))))
        if region.get("elements"):
            facts.append(("Elements", "<br>".join(_element(element) for element in region["elements"][:3])))
        answer = answers.get(region["crop"])
        if answer:
            verdict = ", ".join(str(answer[key]) for key in ("category", "verdict", "confidence") if answer.get(key))
            facts.append(
                ("Image model", f"{escape(str(answer.get('main', '')))} <span class=muted>({escape(verdict)})</span>")
            )
        if crop_id in notes:
            facts.append(("Agent", escape(str(notes[crop_id]))))
        chip = _toward_chip(region, reference) if judged_by is not None else ""
        where = f"({round(left)}, {round(top + offset)})-({round(right)}, {round(bottom + offset)}) CSS px"
        items.append(
            f'<li id="{escape(prefix)}-{escape(name)}-{index}"><div class="region-head">'
            f'<span class="badge">{index + 1}</span>'
            f"{chip}<span class=muted>{where}, {region['differing']} px differ, "
            f"{images.link(base + region['crop'], f'<code>{escape(crop_id)}</code>')}</span></div>"
            f'<div class="crops{" stacked" if stacked else ""}">{"".join(crops)}</div>'
            "<dl>" + "".join(f"<dt>{escape(key)}</dt><dd>{value}</dd>" for key, value in facts) + "</dl></li>"
        )
    rest = len(regions) - len(shown)
    more = ""
    if rest:
        tally = ""
        if judged_by is not None:
            rest_toward = [_toward(region) for region in regions if not region.get("crop")]
            tally = (
                " ("
                + ", ".join(
                    f"{rest_toward.count(toward)} {toward}"
                    for toward in ("further", "closer", "same")
                    if toward in rest_toward
                )
                + ")"
            )
        more = f"<p class=muted>{rest} smaller region(s){tally} are outlined in the differing pixels only.</p>"
    dom_changes = entry.get("dom_changes", [])
    dom = (
        f"<details><summary>{len(dom_changes)} DOM change(s)</summary><ul>"
        + "".join(f"<li>{_dom_change(change)}</li>" for change in dom_changes)
        + "</ul></details>"
        if dom_changes
        else ""
    )
    notes_html = "".join(f"<p class=muted>{escape(note)}</p>" for note in entry.get("notes", []))
    return (
        title
        + notes_html
        + f'<div class="shots">{"".join(shots)}</div>'
        + f'<ol class="regions">{"".join(items)}</ol>'
        + more
        + dom
    )


def _comparison(
    images: Images, prefix: str, test: str, folder: Path | None, result: Result | None, sides: dict[str, Side]
) -> str:
    """The body of a comparison: what it compared, its notes and its differing captures."""
    report = _load(folder / "diff.json") if folder else {}
    head = ""
    if result is not None and result.message and (folder is None or result.status not in ("PASS", "FAIL")):
        head += f"<pre>{escape(result.message)}</pre>"
    if folder is None or not report:
        return head
    head += "<dl>" + "".join(f"<dt>{side.label()}</dt><dd>{escape(side.detail)}</dd>" for side in sides.values())
    head += "</dl>"
    if error := report.get("description_error"):
        head += f"<p>Describing the differences failed: <code>{escape(str(error))}</code></p>"
    triage = _load(folder / TRIAGE)
    if triage:
        agent, verdict, summary = (escape(str(triage.get(key, ""))) for key in ("agent", "verdict", "summary"))
        head += (
            '<div class="note"><strong>Agent\'s conclusion</strong> '
            f"<span class=muted>({agent or 'agent'}, for checking against the pictures)</span>"
            f"<p><strong>{verdict}</strong> {summary}</p></div>"
        )
    vision = _load(folder / VISION)
    answers = {
        answer["crop"]: answer
        for answer in (vision.get("answer") or {}).get("regions", [])
        if isinstance(answer, dict) and answer.get("crop")
    }
    if vision:
        head += f"<p class=muted>Image model answers from {escape(str(vision.get('model')))}.</p>"
    if test == BASELINE:
        panels = [(sides["old"], "expected"), (sides["new"], "actual")]
        if "ref" in sides:
            panels.append((sides["ref"], "reference"))
    else:
        panels = [(sides["ref"], "expected"), (sides["new"], "actual")]
    judged_by = sides.get("ref") if test == BASELINE else None
    captures = report.get("captures", {})
    matching = [name for name, entry in captures.items() if entry.get("matches")]
    body = "".join(
        _capture(images, prefix, folder, name, entry, panels, answers, triage.get("regions", {}), judged_by)
        for name, entry in captures.items()
        if not entry.get("matches")
    )
    same = f"<p class=muted>Same: {escape(', '.join(matching))}</p>" if matching else ""
    return head + same + body


def _rendering(images: Images, folder: Path, data: dict) -> str:
    info = data.get("info", {})
    engine = info.get("engine") or {}
    what = engine.get("user_agent", "") if isinstance(engine, dict) else ""
    facts = [f"<strong>{escape(data['engine'])}</strong> <span class=muted>{escape(str(what))}</span>"]
    if info.get("viewport"):
        facts.append(f"viewport {info['viewport'][0]}x{info['viewport'][1]} CSS px, DPR {info.get('dpr')}")
    if info.get("seconds") is not None:
        facts.append(f"{info['seconds']} s")
    lines = [", ".join(facts)]
    for problem in data.get("problems", []):
        lines.append(f"<span class=FAIL>{escape(problem)}</span>")
    if data.get("error"):
        lines.append(f"<span class=FAIL>{escape(data['error'])}</span>")
    external = info.get("external") or []
    origins = (
        f"<details><summary>{len(external)} resource(s) from other origins</summary><ul>"
        + "".join(f"<li><code>{escape(str(url))}</code></li>" for url in external)
        + "</ul></details>"
        if external
        else ""
    )
    failure = folder / "failure.png"
    shot = (
        f'<img src="{escape(images.src(f"{folder.name}/failure.png", 360))}" alt="failure" style="max-width: 360px">'
        if failure.is_file()
        else ""
    )
    return "<p class=muted>" + "<br>".join(lines) + "</p>" + origins + shot


def _legend(sides: list[dict[str, Side]], checked: bool, judged: bool) -> str:
    """What the report compares: each rendering with the builds and dates of the run, and how to read it. `checked`:
    the run had regression checks; `judged`: a reference judged their changes."""
    by_role: dict[str, list[Side]] = {}
    for found in sides:
        for side in found.values():
            by_role.setdefault(side.role, []).append(side)
    if not by_role:
        return ""
    names = {role: found[0].name for role, found in by_role.items()}
    rows = "".join(
        f"<dt>{found[0].label()}</dt><dd>{escape('; '.join(sorted({side.detail for side in found})))}</dd>"
        for role in ("old", "new", "ref")
        if (found := by_role.get(role))
    )
    reference = names.get("ref", "the reference engine")
    new = names.get("new", "the engine")
    checks = (
        f"Each page's rendering by {new} is compared with {names.get('old', 'its baseline')}'s. A page that renders "
        "as before needs nothing more."
    )
    if checked and judged:
        how = (
            f"{checks} Where it changed, {reference}'s rendering judges each change: one that brings the page closer "
            f"to {reference} is likely an improvement, one that takes it further away likely a regression. The "
            "judgement compares pixels, allowing for content a few pixels apart, and is a hint: check it against the "
            "pictures."
        )
    elif checked:
        how = (
            f"{checks} No reference engine's renderings were stored when the pages were compared, so their changes "
            "aren't judged as improvements or regressions."
        )
    else:
        how = (
            f"Each page's rendering by {new} is compared with {reference}'s. Without baselines, a difference is a gap "
            "between the engines, not a change; known gaps belong in the expectations."
        )
    return f'<div class="legend"><p>{escape(how)}</p><dl>{rows}</dl></div>'


def write_report(artifacts: Path, results_json: Path | None = None, standalone: bool = False) -> Path | None:
    """Writes `report.html` to `artifacts`, a run's artifacts folder, if page scenarios ran there, with the statuses
    from `results_json` (default: the one in `artifacts`), and with `standalone`, or if it's there already, also
    `report-standalone.html`, which has its images inlined. Returns the path of `report.html`."""
    results_json = results_json or artifacts / "results.json"
    tests: dict[tuple[str, str], Path] = {}
    for marker in artifacts.glob(f"*/{TEST_ARTIFACTS_MARKER}"):
        match = KEY.match(marker.read_text(encoding="utf-8").strip())
        if match and match.group(1) in TESTS:
            tests[(match.group(2), match.group(1))] = marker.parent
    renderings: dict[str, list[tuple[Path, dict]]] = {}
    for path in sorted(artifacts.glob("*/rendering.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        renderings.setdefault(data["scenario"], []).append((path.parent, data))
    if not tests and not renderings:
        return None
    report = RunReport.from_file(results_json) if results_json.is_file() else None
    results: dict[tuple[str, str], Result] = {}
    for key, result in (report.results if report else {}).items():
        match = KEY.match(key)
        if match and match.group(1) in TESTS:
            results[(match.group(2), match.group(1))] = result
    scenarios = sorted({scenario for scenario, _ in [*tests, *results]} | set(renderings))
    diffs = {key: _load(folder / "diff.json") for key, folder in tests.items() if key[1] in (BASELINE, REFERENCE)}

    def ran(key: tuple[str, str]) -> bool:
        """Whether the test of `key`, (scenario, test), compared or rendered rather than being skipped."""
        result = results.get(key)
        return bool(diffs.get(key)) or (result is not None and result.status != "SKIP")

    kinds = [test for test in TESTS if any(ran((scenario, test)) for scenario in scenarios)]
    checked = BASELINE in kinds
    sides = {key: _sides(key[1], diff) for key, diff in diffs.items() if diff}
    judged = any("ref" in found for (_, test), found in sides.items() if test == BASELINE)
    verdicts = {
        scenario: _verdict(
            results.get((scenario, BASELINE)),
            diffs.get((scenario, BASELINE), {}),
            sides.get((scenario, BASELINE), {}),
        )
        for scenario in scenarios
    }
    order = list(VERDICTS)
    scenarios.sort(key=lambda scenario: order.index(verdict.key) if (verdict := verdicts[scenario]) else 0)
    names = {side.role: side.name for found in sides.values() for side in found.values()}
    old, new, ref = (
        names.get(role, default) for role, default in (("old", "Old"), ("new", "New"), ("ref", "reference"))
    )
    columns = {RENDERS: "Renders", BASELINE: f"{old} → {new}", REFERENCE: f"{new} against {ref}"}
    head = ("<th>Verdict</th>" if checked else "") + "".join(f"<th>{escape(columns[kind])}</th>" for kind in kinds)
    rows = "".join(
        f'<tr><td><a href="#{escape(scenario)}">{escape(scenario)}</a></td>'
        + (f"<td>{verdict.chip if (verdict := verdicts[scenario]) else ''}</td>" if checked else "")
        + "".join(f"<td>{_status(results.get((scenario, kind)))}</td>" for kind in kinds)
        + "</tr>"
        for scenario in scenarios
    )
    summary = f'<div class="wide"><table><tr><th>Page</th>{head}</tr>{rows}</table></div>'
    counts = {key: sum(1 for verdict in verdicts.values() if verdict and verdict.key == key) for key in VERDICTS}
    tally = (
        '<div class="tally">'
        + "".join(
            f'<span class="verdict v-{key}">{count} × {VERDICTS[key].lower()}</span>'
            for key, count in counts.items()
            if count
        )
        + "</div>"
        if checked
        else ""
    )
    environment = report.environment if report else {}
    device = ", ".join(f"{key} {value}" for key, value in environment.items() if isinstance(value, str | int | float))
    written = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    failed = sum(
        1
        for (_, kind), result in results.items()
        if kind in (BASELINE, REFERENCE) and result.status == "FAIL" and result.unexpected
    )
    triage = (
        f'<p class="note">{failed} comparison(s) failed. An agent can triage them with the skill that comes with '
        "servo-embedder-tests: <code>servo-embedder-tests skill</code> prints where it is.</p>"
        if failed
        else ""
    )

    def render(images: Images) -> str:
        sections = []
        for scenario in scenarios:
            verdict = verdicts[scenario]
            parts = [f'<h2 id="{escape(scenario)}">{escape(scenario)} {verdict.chip if verdict else ""}</h2>']
            if verdict:
                parts.append(f"<p class=why>{escape(verdict.why)}.</p>")
            found = renderings.get(scenario, [])
            parts += [_rendering(images, folder, data) for folder, data in found]
            renders = results.get((scenario, RENDERS))
            explained = any(data.get("problems") or data.get("error") for _, data in found)
            if renders is not None and renders.status != "PASS" and renders.message and not explained:
                parts.append(f"<pre>{escape(renders.message)}</pre>")
            key = (scenario, BASELINE)
            found_sides = sides.get(key, {})
            if verdict is not None and verdict.key not in ("unchanged", "stored"):
                title = f"{found_sides['old'].name} → {found_sides['new'].name}" if found_sides else "Regression check"
                parts.append(f'<h3 id="{escape(scenario)}-baseline">{escape(title)} {_status(results.get(key))}</h3>')
                parts.append(
                    _comparison(images, f"{scenario}-baseline", BASELINE, tests.get(key), results.get(key), found_sides)
                )
            key = (scenario, REFERENCE)
            if ran(key):
                found_sides = sides.get(key, {})
                title = (
                    f"{found_sides['new'].name} against {found_sides['ref'].name}"
                    if found_sides
                    else "Engine comparison"
                )
                body = _comparison(
                    images, f"{scenario}-reference", REFERENCE, tests.get(key), results.get(key), found_sides
                )
                heading = f"{escape(title)} {_status(results.get(key))}"
                if verdict is not None and verdict.key != "new" and body:
                    regions = sum(
                        len(entry.get("regions", []))
                        for entry in diffs.get(key, {}).get("captures", {}).values()
                        if not entry.get("matches")
                    )
                    parts.append(
                        f'<details class="gaps" id="{escape(scenario)}-reference"><summary>{heading} '
                        f"<span class=muted>({regions} differing region(s); the gaps that remain)</span></summary>"
                        f"{body}</details>"
                    )
                else:
                    parts.append(f'<h3 id="{escape(scenario)}-reference">{heading}</h3>{body}')
            sections.append("<section>" + "".join(parts) + "</section>")
        return (
            "<!DOCTYPE html>\n<html lang=en><head><meta charset=utf-8>"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Page scenario report</title><style>{STYLE}</style></head><body>"
            f"<h1>Page scenario report</h1><p class=muted>{escape(device)}"
            f"{' · ' if device else ''}device class {escape(report.device_class if report else '?')} · {written}</p>"
            f"{_legend(list(sides.values()), checked, judged)}{tally}{triage}"
            '<input type="checkbox" id="boxes" checked> <label for="boxes">Show region boxes</label>'
            f"<main>{summary}{''.join(sections)}</main></body></html>\n"
        )

    path = artifacts / REPORT
    path.write_text(render(Images(artifacts)), encoding="utf-8")
    if standalone or (artifacts / STANDALONE).is_file():
        (artifacts / STANDALONE).write_text(render(Images(artifacts, inline=True)), encoding="utf-8")
        _open.cache_clear()
    return path
