---
name: compare-renderings
description: Triage failed servo-embedder-tests page scenarios (test_matches_baseline / test_matches_reference) - decide whether a rendering difference is a regression, an improvement or noise, from diff.json first and, only where that doesn't explain it, by asking an image-capable model about the region crops. Use when a page scenario run has failures, when asked whether new Servo renders GenUI pages worse or better, or before accepting new baselines.
---

# Comparing renderings of page scenarios

A page scenario run compares each capture of a page with the engine's baseline (`test_matches_baseline`) and with a
reference engine such as ArkWeb (`test_matches_reference`). The runner never calls a model. This skill is for the
agent that triages a run. It works for agents without image input: images go to a separate model only when the text
doesn't settle the question.

## 1. Find what failed

- `<artifacts>/results.json`: failed tests with their message. The message names each differing capture and its
  largest region.
- Each failed test's folder (`<artifacts>/page_scenarios_test_pages.py_test_matches_<kind>_<scenario>_/`) has:
  - `diff.json`;
  - `<capture>-compare.png`: expected | actual | differing pixels;
  - `regions/<capture>-<n>.png`: the crop of each region, largest first.
- The renderings are in `<artifacts>/page-scenarios_<engine>_<scenario>/`:
  - `rendering.json`: engine and user agent, viewport, DPR, screenshots per capture, requests to other origins,
    recorded errors;
  - the captures with their DOM snapshots (`<capture>.dom.json`).

## 2. Read diff.json before looking at images

For each capture, `diff.json` gives:

- **`regions`**: boxes in CSS px of the actual capture's viewport, with pixel counts and `elements`. The elements are
  the smallest with text in the region, with path, text and computed `font_family`, `font_size`, `font_weight`,
  `font_style` and `color`.
- **`reading`** of the largest regions, from pixel measurements (`measured`: the offset that best aligns the two
  renderings, the ink of the actual one relative to the expected one, the width of the ink). Trust it, it settles
  most cases without looking:
  - "moved by (dx, dy) CSS px, otherwise nearly the same": a layout shift;
  - "only edge pixels differ": anti-aliasing or a sub-pixel position, usually noise;
  - "wider/narrower with about the same ink": letter spacing or glyph widths;
  - "less ink" / "more ink": thinner or thicker strokes (e.g. bold not drawn bold), or fewer or more marks; "content
    missing" / "content added" when the ink halved or doubled;
  - "image <url> did not load in <engine>", first: an `<img>` in the region has no picture, because its request
    failed or the engine can't decode it. That settles the region; the rest of its reading is the consequence;
  - "shapes differ even at the best alignment": another typeface, other glyphs or missing content. This one needs
    eyes (step 3).
- **`dom_changes`**: elements whose computed style or box changed between the expected and the actual capture's DOM
  snapshot. A style change, or a text box that got wider or narrower, often explains a region outright: another
  fallback font, another weight, changed spacing.
- **`reference`** (baseline tests, when the reference engine's capture is stored): whether the new rendering of the
  region is closer to the reference engine's or further from it. Closer suggests an improvement, further a
  regression. The engines rasterize differently, so it's a hint, not a verdict.
- **`notes`**: viewport or scroll differences, and what part was compared.

Also check:

- `rendering.json` `external`: requests to other origins make renderings depend on the network. Fix the page rather
  than the baseline.
- `rendering.json` `broken_images` (also `test_renders`' failure): images without a picture. If the same image is
  broken in the baseline's or the reference engine's DOM snapshot too, the page is at fault; otherwise the engine
  failed to load or decode it. CSS background images aren't in the DOM: a missing one shows only as "less ink" over
  the element's area.
- Two renderings of the same build normally match exactly, so in a baseline test every reading is a real change,
  however small: "moved by 1 px" is a layout shift to report, not noise. Only "only edge pixels differ" in a region of
  a few pixels, e.g. at an image edge, is noise.

## 3. Ask an image model only for what's left

If you can see images yourself, read the region crops directly and skip this step.

Otherwise, describe the regions with an image-capable model through an OpenAI-compatible API, e.g. OpenAI's models
or DeepSeek V4.1-Flash. The user provides the endpoint, key and model; never guess or reuse other credentials.

```sh
export SERVO_EMBEDDER_TESTS_VISION_BASE_URL=https://api.openai.com/v1   # or https://api.deepseek.com, ...
export SERVO_EMBEDDER_TESTS_VISION_API_KEY=...                          # from the user
export SERVO_EMBEDDER_TESTS_VISION_MODEL=...                            # an image-capable model
python3 skills/compare-renderings/describe_regions.py <test folder> --dry-run      # what would be sent
python3 skills/compare-renderings/describe_regions.py <test folder> --max-regions 4
```

- **Only what the measurements leave open.** Regions whose reading is a shift, anti-aliasing, spacing or an image
  that did not load are not sent (they're listed under `explained`); a test with only those makes no request.
  `--all-regions` sends them too.
- **One request per test.** The script sends the largest `--max-regions` open regions. Each crop shows expected above
  actual above the differing pixels, enlarged; the reading and the text from `diff.json` go with it. It asks for JSON:
  per region a checklist (weight, glyphs, spacing, position, colour, content), the main difference, a category, a
  verdict (regression, improvement, neutral, unclear) and a confidence.
- **Cached.** The answer goes to `vision.json` in the test folder, keyed by the request, so running it again is free.
- **Cost.** Keep to the failing tests and the largest regions, and run `--dry-run` first when unsure.
- **Provider fields.** `--extra '{"max_tokens": 800}'` adds fields some providers want.
- **Which model.** On 11 regions with known causes (DAYU200, 2026-11-10, via OpenRouter):
  - `openai/gpt-5.5` got all right, including text replaced by a gradient box and a missing clip shape, at
    $0.01–0.14 per test;
  - `z-ai/glm-5.3-flashx` got 10, at about $0.003 per test, the same in two runs; `z-ai/glm-5.3-flash` got 9 at
    a third of that, with one answer that wasn't valid JSON;
  - `deepseek/deepseek-v4.1-flash` got 8, at about $0.004 per test. It was reliable on weight and on what the
    measurements state, but missed missing content and clip shapes, and its answers varied between runs.

  On 13 regions of pages not used to design the skill, GPT-5.5 and GLM-5.3-flashx both named a colour emoji drawn
  monochrome and a photo downscaled blurrier; DeepSeek only hinted at them. No model clearly named a missing clip-path
  corner or a missing synthetic italic, so look at the crops of regions that read "shapes differ" when the answer is
  vague. Use GLM-5.3-flashx or a stronger model for those regions.

## 4. Decide, and record it

- **Regression:** report it with the scenario, capture, region, elements and the description, plus the compare
  image's path. Leave the baseline as it is.
- **Improvement, or an intended change:** store the new rendering with a reason, which is kept in the baseline's
  `captures.json`. Get the user's agreement first, since it changes what later runs compare with.
  ```sh
  servo-embedder-tests accept <artifacts> --baselines <dir> <scenario> --reason "<what changed and why it's right>"
  ```
- **A difference from the reference engine** (`test_matches_reference`): it's a Servo bug or a deliberate engine
  difference, not a regression of the run. Report what differs, as for a regression, and propose an expectation for
  `test_matches_reference[<scenario>]` (`update-expectations`) with the `issue` link once there is one, so that the
  test stops failing on it. Don't widen the comparison's tolerance.
- **When the image model's answer and the measurements disagree,** or the answer is vague ("glyphs differ", "font
  fallback") for a region that reads "shapes differ", don't build a verdict on it: say so, and point the user at the
  crop.
- **Unsettled or flaky:** the page animates or depends on the network. Add a `mask`, `[page] freeze = true` or local
  copies of the resources to the scenario. Don't add a tolerance.

Then record your conclusion for the people who check it: write `triage.json` into each test folder you triaged and
write the report again. The report shows it next to the pictures, the image model's answers too, so that a person can
see whether you got it right. Name regions as their crops are named (`regions/page-0-1.png` → `page-0-1`), and say
what you based each note on (the reading, the DOM changes, the image model, the crop).

```json
{"agent": "<model or tool>", "verdict": "regression", "summary": "<one or two sentences>",
 "regions": {"page-0-0": "<what differs and why you think so>"}}
```

```sh
servo-embedder-tests report <artifacts>     # rewrites <artifacts>/report.html
```
