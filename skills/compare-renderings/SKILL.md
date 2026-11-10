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
- Two renderings of the same build normally match exactly. A region of a few pixels at an image edge is noise. A
  region over text is not.

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

- **One request per test.** The script sends the largest `--max-regions` crops, each with the text from
  `diff.json`, and asks for JSON: per region a description, a category (font family, weight, spacing, layout, missing
  content, colour, ...), a verdict (regression, improvement, neutral, unclear) and a confidence.
- **Cached.** The answer goes to `vision.json` in the test folder, keyed by the request, so running it again is free.
- **Cost.** Keep to the failing tests and the largest regions, and run `--dry-run` first when unsure.
- **Provider fields.** `--extra '{"max_tokens": 800}'` adds fields some providers want.

## 4. Decide, and record it

- **Regression:** report it with the scenario, capture, region, elements and the description, plus the compare
  image's path. Leave the baseline as it is.
- **Improvement, or an intended change:** store the new rendering with a reason, which is kept in the baseline's
  `captures.json`. Get the user's agreement first, since it changes what later runs compare with.
  ```sh
  servo-embedder-tests accept <artifacts> --baselines <dir> <scenario> --reason "<what changed and why it's right>"
  ```
- **A known difference from the reference engine:** add an expectation for `test_matches_reference[<scenario>]`
  (`update-expectations`) with an `issue` link. Don't widen the comparison's tolerance.
- **Unsettled or flaky:** the page animates or depends on the network. Add a `mask`, `[page] freeze = true` or local
  copies of the resources to the scenario. Don't add a tolerance.
