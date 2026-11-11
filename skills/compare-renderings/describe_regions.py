#!/usr/bin/env python3
"""Asks an image-capable model, through an OpenAI-compatible chat completions API, what differs in the regions of a
page scenario comparison, so that an agent without image input can triage it.

Sends one request per test with the region crops that the comparison wrote (expected | actual | differing pixels),
largest first, and the text the comparison found for each region. The answer is cached in `vision.json` next to
`diff.json`, keyed by the request, so that asking again costs nothing.

    describe_regions.py <artifacts>/<test folder> [--model M] [--base-url URL] [--max-regions 4] [--dry-run]

The endpoint, key and model come from the options or the environment: SERVO_EMBEDDER_TESTS_VISION_BASE_URL (or
OPENAI_BASE_URL), SERVO_EMBEDDER_TESTS_VISION_API_KEY (or OPENAI_API_KEY) and SERVO_EMBEDDER_TESTS_VISION_MODEL.
`--extra '{"max_tokens": 1000}'` adds fields to the request, for what a provider needs beyond the common ones.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROMPT_VERSION = 2
CATEGORIES = (
    "glyph shapes or font family",
    "font weight or style",
    "text size, spacing or wrapping",
    "layout shift or size",
    "missing or extra content",
    "colour, background or shadow",
    "image or icon",
    "clipping or overflow",
    "anti-aliasing only",
    "other",
)
INSTRUCTIONS = """You compare two renderings of the same web page region for a browser engine test.
Each region comes as one image with three rows, all enlarged by the same factor: "EXPECTED" at the top, "ACTUAL" in
the middle, and at the bottom the pixels that differ, in red on a faded copy of "ACTUAL", with the region outlined in
blue.
Before each image you get measurements made on its pixels. Trust them for position, width and the amount of ink:
"moved by (dx, dy)" means the same content shifted, "only edge pixels differ" means the rows look the same apart from
anti-aliasing, "less ink" means thinner or lighter strokes or fewer marks. Use the image for what measurements can't
tell: stroke weight (bold or regular), typeface (handwriting or plain, serif or sans-serif), glyph shapes, missing or
extra content, colours.
For each region, compare the top and the middle row item by item, "same" or the difference you see: weight, glyphs,
spacing, position, colour, content. Don't report differences that neither the image nor the measurements show.
Then name the main difference, pick the closest category from: CATEGORIES, and judge "ACTUAL" against "EXPECTED":
"regression" (worse), "improvement" (better), "neutral" (as good, or only anti-aliasing) or "unclear".
Answer with JSON only: {"regions": [{"region": <number>, "items": {"weight": "...", "glyphs": "...", "spacing": "...",
"position": "...", "colour": "...", "content": "..."}, "main": "...", "category": "...", "verdict": "...",
"confidence": "low|medium|high"}]}"""


def _setting(option: str | None, *names: str) -> str | None:
    if option:
        return option
    return next((os.environ[name] for name in names if os.environ.get(name)), None)


def _image_url(path: Path, max_width: int) -> str:
    data = path.read_bytes()
    try:
        from PIL import Image
    except ImportError:
        Image = None  # noqa: N806
    if Image is not None:
        with Image.open(io.BytesIO(data)) as image:
            if image.width > max_width:
                image = image.resize((max_width, image.height * max_width // image.width), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                image.save(buffer, "PNG")
                data = buffer.getvalue()
    return "data:image/png;base64," + base64.b64encode(data).decode()


def explained(region: dict) -> bool:
    """Whether the measurements alone explain the region: a shift, anti-aliasing, changed spacing or an image that
    did not load."""
    parts = (region.get("reading") or "").split("; ")
    if parts[0].startswith("image ") and " did not load in " in parts[0]:
        return True
    settled = ("moved by", "only edge pixels differ")
    return bool(parts[0]) and all(part.startswith(settled) or "letter spacing" in part for part in parts)


def regions(test: Path, limit: int, everything: bool = False) -> tuple[list[dict], list[dict], list[str]]:
    """The regions with crops in `test`'s diff.json, largest first, that the measurements don't explain (all of them
    with `everything`), those they do, and the comparison's labels."""
    report = json.loads((test / "diff.json").read_text())
    found = []
    for name, capture in report["captures"].items():
        for region in capture.get("regions", []):
            if region.get("crop"):
                found.append({"capture": name, "dom_changes": capture.get("dom_changes", [])[:5], **region})
    found.sort(key=lambda region: region["differing"], reverse=True)
    settled = [] if everything else [region for region in found if explained(region)]
    open_ = [region for region in found if region not in settled]
    return open_[:limit], settled, report.get("labels", ["expected", "actual"])


def request_body(
    test: Path, model: str, limit: int, max_width: int, extra: dict, everything: bool = False
) -> tuple[dict, list[dict], list[dict]]:
    selected, settled, labels = regions(test, limit, everything)
    instructions = (
        INSTRUCTIONS.replace("CATEGORIES", ", ".join(CATEGORIES))
        .replace("EXPECTED", labels[0])
        .replace("ACTUAL", labels[1])
    )
    content: list[dict] = [{"type": "text", "text": instructions}]
    for number, region in enumerate(selected, 1):
        measurements = dict(region.get("measured") or {})
        if not (region.get("reading") or "").startswith("moved"):
            # The best offset of content that differs in shape means nothing, and models read it as a move.
            measurements.pop("offset", None)
        context = {
            "capture": region["capture"],
            "box_css_px": region["box"],
            "measured": region.get("reading"),
            "measurements": measurements,
            "elements": region.get("elements", []),
            "hint": region.get("reference"),
            "dom_changes_in_capture": region["dom_changes"],
        }
        content.append({"type": "text", "text": f"Region {number}: {json.dumps(context, ensure_ascii=False)}"})
        content.append({"type": "image_url", "image_url": {"url": _image_url(test / region["crop"], max_width)}})
    body = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
    return body, selected, settled


def _parse(text: str) -> object:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                pass
    return {"unparsed": text}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("test", type=Path, help="a test's artifacts folder, with diff.json and regions/")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--max-regions", type=int, default=4, help="regions to send, the largest first")
    parser.add_argument("--max-width", type=int, default=1024, help="crops wider than this are scaled down")
    parser.add_argument("--extra", type=json.loads, default={}, help="JSON object merged into the request")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent, without images")
    parser.add_argument(
        "--all-regions", action="store_true", help="also send regions the measurements explain (shifts, spacing, noise)"
    )
    options = parser.parse_args(argv)
    base_url = _setting(options.base_url, "SERVO_EMBEDDER_TESTS_VISION_BASE_URL", "OPENAI_BASE_URL")
    model = _setting(options.model, "SERVO_EMBEDDER_TESTS_VISION_MODEL")
    key = _setting(None, "SERVO_EMBEDDER_TESTS_VISION_API_KEY", "OPENAI_API_KEY")
    if not (options.test / "diff.json").is_file():
        print(f"error: no diff.json in {options.test}", file=sys.stderr)
        return 2
    body, selected, settled = request_body(
        options.test, model or "<model>", options.max_regions, options.max_width, options.extra, options.all_regions
    )
    explained_by_measurements = [
        {"capture": region["capture"], "box": region["box"], "crop": region["crop"], "measured": region["reading"]}
        for region in settled
    ]
    if not selected:
        note = "the measurements explain every region" if settled else "no differing regions with crops"
        print(json.dumps({"regions": [], "explained": explained_by_measurements, "note": note}, ensure_ascii=False))
        return 0
    if options.dry_run:
        for part in body["messages"][0]["content"]:
            if part["type"] == "image_url":
                part["image_url"]["url"] = f"<{len(part['image_url']['url'])} bytes of PNG>"
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    if not (base_url and model and key):
        print("error: set the endpoint, model and API key (see --help)", file=sys.stderr)
        return 2
    digest = hashlib.sha256(json.dumps([PROMPT_VERSION, base_url, body], sort_keys=True).encode()).hexdigest()
    cache = options.test / "vision.json"
    if cache.is_file():
        cached = json.loads(cache.read_text())
        if cached.get("request") == digest:
            print(json.dumps(cached["answer"], ensure_ascii=False, indent=2))
            return 0
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=options.timeout) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        print(f"error: {error.code} from {base_url}: {error.read().decode(errors='replace')[:1000]}", file=sys.stderr)
        return 1
    answer = _parse(reply["choices"][0]["message"]["content"])
    if isinstance(answer, dict):
        for item in answer.get("regions", []):
            if isinstance(item, dict) and isinstance(item.get("region"), int) and 0 < item["region"] <= len(selected):
                region = selected[item["region"] - 1]
                item.update(capture=region["capture"], box=region["box"], crop=region["crop"])
        answer["explained"] = explained_by_measurements
    record = {"request": digest, "model": model, "usage": reply.get("usage"), "answer": answer}
    cache.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
