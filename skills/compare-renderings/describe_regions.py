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

PROMPT_VERSION = 1
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
INSTRUCTIONS = f"""You compare two renderings of the same web page region for a browser engine test.
Each image has three panels side by side: "{{expected}}" on the left, "{{actual}}" in the middle, and the differing
pixels in red on a faded copy of "{{actual}}" on the right, with the region outlined in blue.
For each region, say concretely what differs between the left and the middle panel (which text, which element,
how), pick the closest category from: {", ".join(CATEGORIES)}, and judge whether "{{actual}}" looks worse
("regression"), better ("improvement"), equally good ("neutral") or you can't tell ("unclear") as a rendering of the
page. Font differences matter: name the visible difference (e.g. bold vs regular, serif vs sans-serif, wider letter
spacing, different glyphs for the same characters).
Answer with JSON only: {{{{"regions": [{{{{"region": <number>, "description": "...", "category": "...",
"verdict": "...", "confidence": "low|medium|high"}}}}]}}}}"""


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


def regions(test: Path, limit: int) -> tuple[list[dict], list[str]]:
    """The regions with crops in `test`'s diff.json, largest first, and the comparison's labels."""
    report = json.loads((test / "diff.json").read_text())
    found = []
    for name, capture in report["captures"].items():
        for region in capture.get("regions", []):
            if region.get("crop"):
                found.append({"capture": name, "dom_changes": capture.get("dom_changes", [])[:5], **region})
    found.sort(key=lambda region: region["differing"], reverse=True)
    return found[:limit], report.get("labels", ["expected", "actual"])


def request_body(test: Path, model: str, limit: int, max_width: int, extra: dict) -> tuple[dict, list[dict]]:
    selected, labels = regions(test, limit)
    instructions = INSTRUCTIONS.format(expected=labels[0], actual=labels[1])
    content: list[dict] = [{"type": "text", "text": instructions}]
    for number, region in enumerate(selected, 1):
        context = {
            "capture": region["capture"],
            "box_css_px": region["box"],
            "elements": region.get("elements", []),
            "hint": region.get("reference"),
            "dom_changes_in_capture": region["dom_changes"],
        }
        content.append({"type": "text", "text": f"Region {number}: {json.dumps(context, ensure_ascii=False)}"})
        content.append({"type": "image_url", "image_url": {"url": _image_url(test / region["crop"], max_width)}})
    body = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
    return body, selected


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
    options = parser.parse_args(argv)
    base_url = _setting(options.base_url, "SERVO_EMBEDDER_TESTS_VISION_BASE_URL", "OPENAI_BASE_URL")
    model = _setting(options.model, "SERVO_EMBEDDER_TESTS_VISION_MODEL")
    key = _setting(None, "SERVO_EMBEDDER_TESTS_VISION_API_KEY", "OPENAI_API_KEY")
    if not (options.test / "diff.json").is_file():
        print(f"error: no diff.json in {options.test}", file=sys.stderr)
        return 2
    body, selected = request_body(
        options.test, model or "<model>", options.max_regions, options.max_width, options.extra
    )
    if not selected:
        print(json.dumps({"regions": [], "note": "no differing regions with crops"}))
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
    record = {"request": digest, "model": model, "usage": reply.get("usage"), "answer": answer}
    cache.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(answer, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
