"""The page scenario report: `report.html` in a run's artifacts folder, for people to see what differs.

Per scenario it shows each differing capture next to what it was compared with, with the differing regions numbered,
their enlarged crops and what the comparison measured. It tells the two kinds of comparison apart: a regression check
(an engine against its own baselines, e.g. after an update) and an engine comparison (against another engine's
renderings, its gaps). Where an agent triaged the run, it adds the image model's
answers (`vision.json`, from the compare-renderings skill) and the agent's conclusion (`triage.json`), so that people
can check them against the pictures:

    {"agent": "<who>", "verdict": "regression", "summary": "<text>", "regions": {"<capture>-<n>": "<text>"}}

`<capture>-<n>` names a region as its crop does (`regions/<capture>-<n>.png`).
"""

from __future__ import annotations

import datetime
import json
import re
from html import escape
from pathlib import Path

from .expectations import Result, RunReport
from .plugin import TEST_ARTIFACTS_MARKER

REPORT = "report.html"
TRIAGE = "triage.json"
VISION = "vision.json"
TESTS = {"test_renders": "renders", "test_matches_baseline": "baseline", "test_matches_reference": "reference"}
# The comparisons: their name, CSS class and what a difference means.
KINDS = {
    "test_matches_baseline": (
        "Regression check",
        "regression",
        "A difference is a change since the baselines were stored: a regression, or an improvement to accept as the "
        "new baseline.",
    ),
    "test_matches_reference": (
        "Engine comparison",
        "gap",
        "A difference is a gap between the two engines, not a change; known gaps belong in the expectations.",
    ),
}
KEY = re.compile(r"^page_scenarios/test_pages\.py::(test_\w+)\[(.+)\]$")
SERVO = re.compile(r"\bServo/(\S+)")

STYLE = """
:root {
  --bg: #ffffff; --fg: #1d1d1f; --muted: #6b6b70; --line: #dcdce0; --card: #f5f5f7; --pass: #1a7f37;
  --fail: #c62828; --skip: #75757a; --box: #1565c0; --note: #fff7d6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a; --fg: #e8e8ea; --muted: #9a9aa0; --line: #34353b; --card: #1f2024; --pass: #57b75b;
    --fail: #ef5350; --skip: #9a9aa0; --box: #64b5f6; --note: #2c2814;
  }
}
body { margin: 0 auto; padding: 16px; max-width: 1600px; background: var(--bg); color: var(--fg);
  font: 14px/1.45 system-ui, sans-serif; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 32px 0 8px; padding-top: 12px; border-top: 2px solid var(--line); }
h3 { font-size: 16px; margin: 20px 0 8px; }
h4 { font-size: 14px; margin: 16px 0 8px; }
code { font-size: 12px; }
.muted { color: var(--muted); }
.status { font-weight: 600; }
.PASS { color: var(--pass); } .FAIL, .TIMEOUT, .CRASH { color: var(--fail); } .SKIP { color: var(--skip); }
.unexpected::after { content: " (unexpected)"; font-weight: 400; }
table { border-collapse: collapse; margin: 8px 0; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
.shots { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-start; }
figure { margin: 0; min-width: 0; max-width: 100%; }
.wide { overflow-x: auto; }
figcaption { font-size: 12px; color: var(--muted); margin-top: 2px; }
.shot { position: relative; line-height: 0; border: 1px solid var(--line); max-width: 100%; }
.shot img { width: 100%; height: auto; }
.flip .top { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
.flip:hover .top { visibility: hidden; }
.box { position: absolute; border: 2px solid var(--box); box-sizing: border-box; }
.box span { position: absolute; top: -2px; left: -2px; padding: 2px 4px; background: var(--box); color: #fff;
  font: 600 11px/1 system-ui, sans-serif; }
#boxes:not(:checked) ~ main .box { display: none; }
.regions { list-style: none; padding: 0; display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 420px), 1fr)); }
.regions li { background: var(--card); border-radius: 6px; padding: 8px; }
.regions img { max-width: 100%; max-height: 720px; height: auto; }
.badge { display: inline-block; min-width: 18px; padding: 1px 5px; background: var(--box); color: #fff;
  border-radius: 3px; text-align: center; font-weight: 600; }
dl { margin: 6px 0 0; display: grid; grid-template-columns: max-content 1fr; gap: 2px 10px; }
dt { color: var(--muted); }
dd { margin: 0; }
.note { background: var(--note); border-radius: 6px; padding: 8px 10px; margin: 8px 0; }
.note p { margin: 4px 0; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--card); padding: 8px; border-radius: 6px; }
.kind { display: inline-block; padding: 3px 6px; border-radius: 3px; color: #fff;
  font: 600 11px/1 system-ui, sans-serif; text-transform: uppercase; letter-spacing: 0.04em; vertical-align: 2px; }
.kind.regression { background: #8a5a00; } .kind.gap { background: #5b3fa0; }
.overview { list-style: none; padding: 0; margin: 8px 0; } .overview li { margin: 4px 0; }
details { margin: 4px 0; } summary { cursor: pointer; }
"""


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


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


def _who(info: object, label: str = "") -> str:
    """An engine and its version, from what a rendering recorded about it (the name --engine-version gave its build
    first); the engine's name only where it isn't `label`."""
    if not isinstance(info, dict):
        return ""
    name = str(info.get("engine", ""))
    if info.get("arkweb"):
        detected = f"ArkWeb {info['arkweb']}" + (f", Chromium {info['chromium']}" if info.get("chromium") else "")
    elif match := SERVO.search(str(info.get("user_agent", ""))):
        detected = f"Servo {match.group(1)}"
    else:
        detected = str(info.get("user_agent", ""))
    version = " · ".join(part for part in (str(info.get("version") or ""), detected) if part)
    if name == label:
        return version or name
    return f"{name} ({version})" if version else name


def _origin(expected: object) -> tuple[object, str]:
    """The engine of the captures a comparison compared with, and when they were stored or that they were rendered in
    the run."""
    if not isinstance(expected, dict):
        return None, ""
    if not isinstance(expected.get("engine"), dict):
        return expected, ""  # older diff.json: the baseline's engine only
    when = "rendered in this run" if expected.get("from") == "rendered in this run" else ""
    if expected.get("stored"):
        when = f"stored {str(expected['stored'])[:10]}" + (f" ({expected['reason']})" if expected.get("reason") else "")
    return expected["engine"], when


def _sides(report: dict) -> str:
    """What a comparison compared: both engines with their versions, and when the expected captures were stored."""
    labels = report.get("labels", ["expected", "actual"])
    engine, when = _origin(report.get("expected"))
    expected = ", ".join(part for part in (_who(engine, labels[0]), when) if part)
    actual = _who(report.get("actual"), labels[1])
    if not expected and not actual:
        return ""
    return f"{escape(labels[0])}: {escape(expected or '?')} · {escape(labels[1])} now: {escape(actual or '?')}"


def _overview(comparisons: dict[str, list[dict]]) -> str:
    """What the run compared, per kind of comparison, and what a difference means for each."""
    items = []
    for test, reports in comparisons.items():
        title, css, meaning = KINDS[test]
        labelled = [report.get("labels", ["expected", "actual"]) for report in reports]
        if test == "test_matches_baseline":
            now = sorted(
                {_who(report.get("actual")) or labels[1] for labels, report in zip(labelled, reports, strict=True)}
            )
            then = sorted({who for report in reports if (who := _who(_origin(report.get("expected"))[0]))})
            dates = sorted(
                {
                    str(expected["stored"])[:10]
                    for report in reports
                    if isinstance(expected := report.get("expected"), dict) and expected.get("stored")
                }
            )
            what = f"{', '.join(now) or 'The engine'} against its baselines"
            if then and then != now:
                what += f" of {', '.join(then)}"
            if dates:
                what += f", stored {', '.join(dates)}"
        else:
            pairs = sorted(
                {
                    f"{labels[1]} against {labels[0]}"
                    + (f" ({who})" if (who := _who(_origin(report.get("expected"))[0], labels[0])) else "")
                    for labels, report in zip(labelled, reports, strict=True)
                }
            )
            what = "; ".join(pairs) or "The engine against another engine"
        items.append(f'<li><span class="kind {css}">{title}</span> {escape(what)}. {meaning}</li>')
    return f'<ul class="overview">{"".join(items)}</ul>' if items else ""


def _shot(images: list[tuple[str, str]], width: float, boxes: str, caption: str) -> str:
    """A capture image, or two that flip on hover, with the region boxes over it."""
    flip = len(images) > 1
    tags = "".join(
        f'<img src="{escape(src)}" alt="{escape(alt)}"{" class=top" if flip and index else ""}>'
        for index, (src, alt) in enumerate(images)
    )
    return (
        f'<figure><div class="shot{" flip" if flip else ""}" style="width: {width:g}px">{tags}{boxes}</div>'
        f"<figcaption>{caption}</figcaption></figure>"
    )


def _capture(prefix: str, folder: Path, name: str, entry: dict, labels: list[str], answers: dict, notes: dict) -> str:
    title = f"<h4>{escape(name)}: {escape(str(entry.get('summary', '')))}</h4>"
    images = entry.get("images")
    if not images:
        compare = entry.get("compare")
        return title + (f'<img src="{escape(folder.name)}/{escape(compare)}" alt="">' if compare else "")
    base = f"{folder.name}/"
    width, height = entry["size"]
    offset = entry["offset"]
    regions = entry.get("regions", [])
    shown = [(index, region) for index, region in enumerate(regions) if region.get("crop")]
    boxes = ""
    for index, region in shown:
        left, top, right, bottom = region["box"]
        style = (
            f"left: {100 * left / width:.2f}%; top: {100 * (top - offset) / height:.2f}%; "
            f"width: {100 * (right - left) / width:.2f}%; height: {100 * (bottom - top) / height:.2f}%"
        )
        boxes += f'<a class="box" href="#{prefix}-{escape(name)}-{index}" style="{style}"><span>{index + 1}</span></a>'
    expected, actual = (escape(label) for label in labels)
    shots = [
        _shot([(base + images["expected"], labels[0])], width, boxes, expected),
        _shot(
            [(base + images["expected"], labels[0]), (base + images["actual"], labels[1])],
            width,
            boxes,
            f"{actual} <span class=muted>(hover: {expected})</span>",
        ),
        _shot([(base + images["differing"], "differing pixels")], width, boxes, "differing pixels in red"),
    ]
    items = []
    for index, region in shown:
        crop_id = Path(region["crop"]).stem
        left, top, right, bottom = (round(value) for value in region["box"])
        facts = [("Measured", escape(str(region.get("reading") or "")))]
        if region.get("reference"):
            facts.append(("Reference", escape(region["reference"])))
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
        items.append(
            f'<li id="{prefix}-{escape(name)}-{index}"><span class="badge">{index + 1}</span> '
            f"<code>{escape(crop_id)}</code> "
            f"<span class=muted>({left}, {top})-({right}, {bottom}) CSS px, {region['differing']} px differ</span>"
            f'<div><img src="{escape(base + region["crop"])}" alt="region {index + 1}" loading="lazy"></div>'
            "<dl>" + "".join(f"<dt>{key}</dt><dd>{value}</dd>" for key, value in facts) + "</dl></li>"
        )
    rest = len(regions) - len(shown)
    more = f"<p class=muted>{rest} smaller region(s) are outlined in the differing pixels only.</p>" if rest else ""
    changes = entry.get("dom_changes", [])
    dom = (
        f"<details><summary>{len(changes)} DOM change(s)</summary><ul>"
        + "".join(f"<li>{_dom_change(change)}</li>" for change in changes)
        + "</ul></details>"
        if changes
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


def _comparison(prefix: str, test: str, folder: Path | None, result: Result | None) -> str:
    title, css, _ = KINDS[test]
    badge = f'<span class="kind {css}">{title}</span>'
    report = _load(folder / "diff.json") if folder else {}
    head = f'<h3 id="{prefix}">{badge} {_status(result)}</h3>'
    if result is not None and result.message and (folder is None or result.status not in ("PASS", "FAIL")):
        head += f"<pre>{escape(result.message)}</pre>"
    if folder is None or not report:
        return head
    labels = report.get("labels", ["expected", "actual"])
    against = "its baseline" if test == "test_matches_baseline" else labels[0]
    head = f'<h3 id="{prefix}">{badge} {escape(labels[1])} against {escape(against)} {_status(result)}</h3>'
    if sides := _sides(report):
        head += f"<p class=muted>{sides}</p>"
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
    captures = report.get("captures", {})
    matching = [name for name, entry in captures.items() if entry.get("matches")]
    body = "".join(
        _capture(prefix, folder, name, entry, labels, answers, triage.get("regions", {}))
        for name, entry in captures.items()
        if not entry.get("matches")
    )
    same = f"<p class=muted>Same: {escape(', '.join(matching))}</p>" if matching else ""
    return head + same + body


def _rendering(folder: Path, data: dict) -> str:
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
        f'<img src="{escape(folder.name)}/failure.png" alt="failure" style="max-width: 360px">'
        if failure.is_file()
        else ""
    )
    return "<p>" + "<br>".join(lines) + "</p>" + origins + shot


def write_report(artifacts: Path, results_json: Path | None = None) -> Path | None:
    """Writes `report.html` to `artifacts`, a run's artifacts folder, if page scenarios ran there, with the statuses
    from `results_json` (default: the one in `artifacts`). Returns its path."""
    results_json = results_json or artifacts / "results.json"
    tests: dict[tuple[str, str], Path] = {}
    for marker in artifacts.glob(f"*/{TEST_ARTIFACTS_MARKER}"):
        match = KEY.match(marker.read_text(encoding="utf-8").strip())
        if match and match.group(1) in TESTS:
            tests[(match.group(2), match.group(1))] = marker.parent
    renderings: dict[str, list[tuple[Path, dict]]] = {}
    for path in sorted(artifacts.glob("*/rendering.json")):
        data = json.loads(path.read_text())
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
    kinds = [test for test in TESTS if any(kind == test for _, kind in [*tests, *results])]
    rows = "".join(
        f'<tr><td><a href="#{escape(scenario)}">{escape(scenario)}</a></td>'
        + "".join(f"<td>{_status(results.get((scenario, kind)))}</td>" for kind in kinds)
        + "</tr>"
        for scenario in scenarios
    )
    head = "".join(f"<th>{KINDS[kind][0].lower() if kind in KINDS else TESTS[kind]}</th>" for kind in kinds)
    summary = f'<div class="wide"><table><tr><th>Scenario</th>{head}</tr>{rows}</table></div>'
    comparisons = {
        kind: [
            diff
            for (_, test), folder in sorted(tests.items())
            if test == kind and (diff := _load(folder / "diff.json"))
        ]
        for kind in kinds
        if kind in KINDS
    }
    sections = []
    for scenario in scenarios:
        parts = [f'<h2 id="{escape(scenario)}">{escape(scenario)}</h2>']
        found = renderings.get(scenario, [])
        parts += [_rendering(folder, data) for folder, data in found]
        renders = results.get((scenario, "test_renders"))
        explained = any(data.get("problems") or data.get("error") for _, data in found)
        if renders is not None and renders.status != "PASS" and renders.message and not explained:
            parts.append(f"<pre>{escape(renders.message)}</pre>")
        for kind in ("test_matches_baseline", "test_matches_reference"):
            folder = tests.get((scenario, kind))
            if folder is not None or (scenario, kind) in results:
                prefix = f"{scenario}-{TESTS[kind]}"
                parts.append(_comparison(prefix, kind, folder, results.get((scenario, kind))))
        sections.append("<section>" + "".join(parts) + "</section>")
    environment = report.environment if report else {}
    device = ", ".join(f"{key} {value}" for key, value in environment.items() if isinstance(value, str | int | float))
    written = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    page = (
        "<!DOCTYPE html>\n<html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Page scenario report</title><style>{STYLE}</style></head><body>"
        f"<h1>Page scenario report</h1><p class=muted>{escape(device)}"
        f"{' · ' if device else ''}device class {escape(report.device_class if report else '?')} · {written}</p>"
        f"{_overview(comparisons)}"
        '<input type="checkbox" id="boxes" checked> <label for="boxes">Show region boxes</label>'
        f"<main>{summary}{''.join(sections)}</main></body></html>\n"
    )
    path = artifacts / REPORT
    path.write_text(page, encoding="utf-8")
    return path
