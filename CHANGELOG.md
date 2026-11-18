# Changelog

## Unreleased

- The page scenario report reads as a three-way comparison: each page gets a verdict (unchanged, likely regression,
  likely improvement, mixed, unclear) from whether its changes since the baseline moved toward the reference engine's
  rendering or away from it. Changed captures and their regions show the old rendering, the new one and the
  reference's side by side; the engine comparison is folded away where there is a baseline. Renderings are named
  after their engine and role, e.g. Old Servo, New Servo and ArkWeb, with their builds.
- Whether a change moved toward the reference engine's rendering allows for content laid out up to 24 CSS px apart
  and compares colours; before, a blank region counted as closer to the reference's text than text a few pixels off.
  Regions in `diff.json` have `toward_reference` (closer, further, same or null), baseline tests record where the
  reference came from as `reference` and write `<capture>-reference.png`. References with a viewport of another
  width aren't compared.

## 0.2.0

Changes that can need action:

- A missing `--expectations` file is an error; before, the run expected every test to pass.
- Statuses: a WebDriver command that gets no answer is TIMEOUT, not FAIL; servoshell exiting at start-up is CRASH,
  not FAIL; failing to start it is FAIL, not CRASH. Expectations may need updating.
- New dependencies `opencv-python-headless` and `numpy`.

Other changes:

- Page scenario comparisons pass or fail at once and describe their differences in the background, 2.5 times as fast;
  `diff.json` is complete when the run ends, with `description_error` where describing failed or was stopped.
- Ink readings count content wider than strokes, such as a thick stroke, as ink.
- WebDriver connections to servoshell and chromedriver ignore `http_proxy`.
