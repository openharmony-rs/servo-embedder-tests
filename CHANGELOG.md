# Changelog

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
