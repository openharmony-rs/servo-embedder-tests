# servo-embedder-tests

End-to-end tests for [Servo](https://servo.org) embedders, run against servoshell.
This is meant as an addition to wpt, covering specific embedder usecases such as 
- Starting the app and navigating through real pages
- Using OS-native input (e.g. touch, native IME)

This is not an official servo project.
As of now the project is mostly focused on the OpenHarmony experience, but the scope might
be expanded.

## Running

```sh
uvx servo-embedder-tests doctor                      # hdc, device, installed app, mitmdump
uvx servo-embedder-tests run                         # bundled suite on the only hdc device
uvx servo-embedder-tests run --hdc-target <serial> --hap servoshell-default-signed.hap navigation/
uvx servo-embedder-tests run sites/test_mossel.py::test_mossel_home
uvx servo-embedder-tests run --platform desktop --servo-bin target/release/servoshell
uvx servo-embedder-tests list
```

From Python:

```python
import servo_embedder_tests

exit_code = servo_embedder_tests.run_tests(["--hdc-target", "<serial>", "--expectations", "expectations.toml"])
```

`run` takes any pytest option. See `--help` for all options.

The IME scenarios need the test IME from ohos-ime, built as a feature module of servoshell's bundle
(signed with servoshell's profile) or standalone: `run --test-ime org.servo.servo`.

The `webview_apps/` scenarios drive apps that show their pages in the system web view rather than servoshell, and run
only when named: `run webview_apps/`. They open each tab, drag its page up and back down, open a page from a tab and
go back. What they test is the system web view, so they are meant to compare web engines on one device: they only
check what the platform shows (the ArkUI tree, web view URLs, screenshots, processes and the system's crash events)
and give every run the same taps and fling-free drags. Give each engine a device class of its own, e.g.
`--device-class harmonyos-servo`, which falls back to `harmonyos` expectations; results.json records the ArkWeb
version. An app is skipped unless installed; the scenarios accept a terms dialog it shows on first start.

The apps are defined in TOML: bundled is [arkweb-test](https://github.com/jschwe/arkweb-test), whose tabs page shows
three web views (on OpenHarmony, `param set web.engine.enforce` picks the engine they all use), and
`--webview-apps FILE` adds more. Each `[[app]]` has a `name`, `bundle`, `label`, `install` hint, the `home` screen
and optionally how to `start` it (a page of the harness or a URL, `--ps` parameters, the ability), then a
`[[app.tab]]` per tab and a `[[app.visit]]` per page to open from a tab (see
`scenarios/webview_apps/apps/arkweb-test.toml` and `flows.load`):

```toml
[[app]]
name = "shop"
bundle = "com.example.shop"
label = "Shop"
install = "install Shop from the app store"
home = { url = "/pages/index.html", tab = "Home" }   # the web view's URL ends with url; the selected tab
start = { ability = "MainAbility", parameters = { region = "eu" } }   # by default EntryAbility, no parameters

[[app.tab]]
name = "home"
label = "Home"
url = "/pages/index.html"

[[app.visit]]
name = "product"
tab = "home"
target = [0.5, 0.3]                                  # where to tap, as fractions of the page
screen = { url = "/pages/product.html" }
description = "the first product's page"
```

They need no WebDriver, so they work with any engine of the system web view. An app works with them when:

- `aa start -a <ability> -b <bundle>`, with `-U` and `--ps` from `start`, cold-starts it into the `home` screen;
- its pages are ArkUI `Web` components, and the last `Web` in the layout is the page in front. The `text` ArkUI
  reports for it, its URL, ends with the screen's `url`. Some apps report the URL the web view was created with,
  which doesn't change when it navigates (arkweb-test does): their visits can't tell the opened page by its URL;
- its tabs are an ArkUI `TabBar` whose items are `selected` when shown and labelled with the tab's `label`;
- each tab's page scrolls under a slow drag, isn't a single flat colour (the luminance's standard deviation is above
  15) and stops changing, apart from small animations, within 20 s;
- Back closes a visited page and shows the tab's page again;
- nothing stays in front of it but the system's terms dialog, which the scenarios accept.

Their checks are coarse, so a run is meant to be reviewed, by a person or an agent. Each test's artifacts folder has
`overview.jpeg`, the steps side by side; `steps.json`, every step's screenshot and layout dump with what it should
show (`expect`) and what the checks measured; `faults.json`, the system's fault events during the test; and
`failure.jpeg` when a check failed. Look for blank or half-rendered pages, spinners and error pages, missing images,
broken layout or text, and screenshots that don't show what `expect` says, above all in steps with a `review` note,
which the checks couldn't judge. To compare two runs, pair the steps by test and name; apps with live content show
other feeds, campaigns and search hints in every run anyway.

Expectations work like WPT metadata. A TOML table per test maps a device class (or `default`) to the
status, or list of statuses, the run may produce: `PASS`, `FAIL`, `TIMEOUT`, `CRASH` or `SKIP`.
A test without an entry must pass. An expected failure is reported as xfail.

```toml
["sites/test_mossel.py::test_mossel_slide"]
harmonyos = ["TIMEOUT"]
issue = "https://github.com/servo/servo/issues/12345"
```

The detected device classes are `harmonyos`, `openharmony` and `openharmony-emulator` (QEMU images such as
those of `cargo ohos emulator`); `--device-class` overrides the detection.
Lookup tries the device class, then its prefixes (`openharmony-emulator` → `openharmony`), then `default`.
After a run, `servo-embedder-tests update-expectations <artifacts>/results.json --expectations expectations.toml`
rewrites the file to match, leaving entries and comments in place. `--intermittent` adds statuses instead of
replacing them.

## Page scenarios

A folder with a subfolder per page, each with its `index.html` and resources, becomes a set of scenarios with
`--page-scenarios <folder>`. Without test names, `run` then runs only those:

```sh
# The first run stores what the engine renders as the baselines; --engine-version names the build.
uvx servo-embedder-tests run --page-scenarios genui --baselines genui-baselines --update-baselines \
    --engine-version v0.8.0
# Later runs, e.g. after a Servo update, compare with them.
uvx servo-embedder-tests run --page-scenarios genui --baselines genui-baselines --expectations genui.toml \
    --engine-version main@3f2a1b
# Also compare with ArkWeb on the same device, through chromedriver over ArkWeb's DevTools socket.
uvx servo-embedder-tests run --page-scenarios genui --baselines genui-baselines --reference-engine arkweb \
    --arkweb-viewer <bundle> --chromedriver <chromedriver matching ArkWeb's Chromium>
# Store a changed rendering, after review, as the new baseline (by default those whose baseline test failed).
uvx servo-embedder-tests accept servo-embedder-tests-artifacts --baselines genui-baselines --reason "fixes bold"
```

Each scenario has three tests:

- `test_renders`: the page loads, settles, every `<img>` loaded a picture, and the page passes its own checks;
- `test_matches_baseline`: each capture matches the engine's baseline;
- `test_matches_reference`: each capture matches the reference engine's.

The first is a regression check: it finds what changed since the baselines were stored, e.g. with an engine update.
The second is an engine comparison: it finds the gaps between two engines. `--engine-version NAME` and
`--reference-engine-version NAME` name the builds, e.g. a commit or a release. The name is kept with their captures
and baselines, so that `diff.json` and the report say which builds were compared. Stored references keep the name
they were rendered with: `--reference-engine-version` names those rendered in the run, see `--update-references`.

The page is rendered once per engine for all three. Known differences between engines go into the expectations of
`test_matches_reference`, so that they don't hide regressions. The captures of each engine are kept in
`--baselines` under `<device>/<engine>/<scenario>/`; the device is its model, or `--reference-key`. The reference
engine's captures are expectations like the baselines: with them stored, a run doesn't need the reference engine. It
renders the missing ones if it has the engine, and `--update-references` renders all anew.

A capture is a screenshot of the web view, taken by the OS the same way for every engine. It's taken after:

- the page loaded, with its images and fonts;
- no resource has loaded for a while;
- two screenshots in a row are the same.

If the page keeps changing, the failure names what changes and the elements there. Each capture keeps a DOM
snapshot: the boxes, text and fonts of the elements with text, whether each image loaded, and paint properties such
as `background-clip: text`, `clip-path` and background images where set.

Captures are compared in tiles of 16 CSS px. A tile differs when more than 5% of its pixels differ by more than the
tolerance, so that a heading in another font fails even on a long page. Engines are compared in CSS pixels with a
higher tolerance for anti-aliasing. Captures of viewports with other heights or scroll positions are compared where
they show the same part of the page.

Each comparison test writes `diff.json` with, for each capture:

- the differing regions in the actual capture's viewport, in CSS px;
- the elements whose boxes best match each region, with their text, computed fonts and paint properties;
- for the largest regions, a `reading` of pixel measurements: moved by how much, only anti-aliasing, wider with
  the same ink (spacing), less or more ink (stroke weight, or content missing), shapes that differ even when
  aligned, or a colour drawn in another colour (such as a colour emoji drawn black). It starts with the images in the
  region that did not load, if any;
- `dom_changes`, the elements whose text, box, style or image load state changed;
- for baselines, whether each region moved closer to the reference engine's rendering or further from it, a hint
  whether a change is an improvement.

It also writes `<capture>-compare.png` (expected, actual and the differing pixels) and a crop of each region in
`regions/`, expected above actual above the differing pixels. Those crops are what to send to an image-capable model
for what the readings leave open: the skill in `skills/compare-renderings` describes how an agent triages a run, and
its script asks any OpenAI-compatible endpoint about the regions.

For people, a run writes `report.html` to the artifacts folder: per scenario, each differing capture next to what it
was compared with (hover the actual one to see the expected one in its place), the differing pixels, the regions
numbered with their enlarged crops and readings, and the DOM changes. Where an agent triaged the run, the report also
shows the image model's answers and the agent's conclusion (`triage.json` in the test's folder, see the skill), so
that you can check them against the pictures. `servo-embedder-tests report <artifacts>` writes it again.

`page-scenarios.toml` in the folder sets defaults for all scenarios, `scenario.toml` in a scenario's folder
overrides them. Changes to the page are opt-in, under `[page]`:

```toml
segments = 5                  # viewports to capture from the top; "all" scrolls to the end
segment_height = 0            # CSS px between captures (0: the viewport height); set it to the smaller
                              # viewport height when engines with different viewports are compared
mask = [".clock"]             # elements that comparisons ignore
timeout = 30                  # seconds to load, and for each capture to settle

[page]
freeze = true                 # a style sheet before the page's own: no CSS animations or transitions
errors = true                 # a script that records errors, failed resources and console.error
seed_random = true            # Math.random() returns the same numbers on every load and in every engine

[compare.baseline]            # overrides of the presets: tolerance, tile, tile_share, css
tile_share = 0.05

[[steps]]                     # instead of the viewport captures
capture = "initial"
[[steps]]
hover = "#buy"                # also click, scroll ("top", "bottom", a number or a selector), wait_for
[[steps]]
capture = "hover"
expect_change = "#buy"        # fails unless the capture differs from the previous one there
```

`seed_random` replaces the page's `Math.random()` with a seeded generator. Its numbers repeat as long as the page asks
for them in the same order: calls from timers, animation frames or load events whose order or number varies from load
to load, e.g. a script that adds a random star every frame, still differ; workers and `crypto.getRandomValues()` stay
random.

The `arkweb` engine and the reference captures take any app as `--arkweb-viewer`, started with its `EntryAbility`
or `--arkweb-viewer-ability`, and with `--arkweb-viewer-ps NAME VALUE` parameters. The app needs to:

- show `<url>` in an ArkWeb `Web` component when `aa start -a <ability> -b <bundle> -U <url>` cold-starts it, and
  come to the front when started again while it runs;
- show the page in the last `Web` of the layout, with nothing over it (toolbars, banners, dialogs, the keyboard),
  and not move or resize it while it runs: captures are screenshots of the screen, cut to where the layout puts it;
- load the harness's pages from `http://127.0.0.1:<port>/` (forwarded with hdc): cleartext HTTP, JavaScript on, no
  proxy of its own;
- run in one process, which `aa force-stop` ends.

For the `arkweb` engine, ArkWeb must run Chromium, and the app should show a single web view: chromedriver attaches
through the DevTools socket that `param set web.debug.devtools true` gives every web view, so the app needn't enable
debugging, but nothing chooses which web view it drives. To compare with another engine, the web view should be as
wide, in CSS pixels, as that engine's: only the part of the page both show is compared, and other widths lay the
page out differently.

Engines other than `servoshell` and `arkweb`, e.g. an app whose web view library switches between ArkWeb and Servo,
come from a plugin, a `conftest.py` or a module passed with `-p`. They need a WebDriver session into the web view
for navigation, scripts (also asynchronous ones), elements by CSS selector, clicks and pointer moves; screenshots
come from the OS:

```python
from servo_embedder_tests.engines import ArkWebEngine


class MyAppArkWeb(ArkWebEngine):
    name = "myapp-arkweb"


def pytest_servo_embedder_tests_engines(config, platform):
    # Engines implement servo_embedder_tests.engines.Engine: open(url) -> WebDriver, screen_box(), describe(), ...
    return [MyAppArkWeb(platform, "com.example.myapp", config.option.chromedriver, config.option.artifacts / "cd.log")]
```

## Writing scenarios

Fixtures:

| Fixture | Provides |
| --- | --- |
| `servo` | servoshell on `about:blank` with a WebDriver session; `servo.driver`, `servo.start(url, prefs, args, webdriver)` |
| `webdriver` | `servo.driver` |
| `pages` | local HTTP server for fixture pages, reachable from the device: `pages.url("basic.html")`, `pages.add(path, html)` |
| `device` | the hdc device (skips on desktop): `shell`, `tap`, `swipe`, `key`, `screenshot`, `dump_layout` |
| `hilog` | hilog of the run, streamed from the first test that uses it: `wait_for(regex, since=hilog.mark())` |
| `proxy` | the mitmproxy instance for `network` tests |
| `ime` | the test IME, current for the test (`ohos_test_ime.TestIme`); skips without `--test-ime` |
| `artifacts` | the test's artifacts folder, for screenshots and other files to keep whatever the result |
| `page_runs` | renderings of page scenarios by engine (`servo_embedder_tests.page_scenarios.PageRuns`) and their stores |

Pages that load `report.js` can call `report({...})`; tests read the objects with
`pages.wait_for_report(predicate)`. That works without WebDriver, e.g. for IME scenarios, where
servoshell must run as a user would.

Markers: `network`, `platform("ohos")`, `servo_prefs(name=value)`, `servo_args("--flag")`, `explicit`.

`servo_embedder_tests.touch` turns a WebDriver element into a screen point and taps it through the OS. Use
it when the test is about the platform path: WebDriver clicks enter Servo below ArkUI.

`servo_embedder_tests.webview_app.WebViewApp` drives an app through `uitest` and `uinput`:
`capture(name, screen, expect)` waits for a page (`Screen(url=…, tab=…, texts=…)`) to settle and keeps its screenshot
and layout as a step; `tap(Point(x, y))` and `drag(fraction)` act relative to the last step's page. Mark tests that
drive other apps `explicit`, so that they stay out of the default run.

Rules: no bare sleeps (use `servo_embedder_tests.oracles.wait_until`, `hilog.wait_for` or WebDriver waits; the
Mossel scenarios ported from Servo still have some), local fixture pages unless the test is about a real site, one
behaviour per test, and assert the intended behaviour, not today's.

## Development

```sh
just test    # host-only unit tests
just lint
just run --hdc-target <serial>
```

## License

Apache-2.0 OR MIT. Some scenarios and the mitmproxy options are ported from Servo's `etc/ci/scenario` and
keep their notices. The SVG test pages in `pages/svg/` come from
[svg-tests-servo](https://github.com/mu-mostafa98/svg-tests-servo) by Mohamed Mostafa and keep its MIT license, in
`pages/svg/LICENSE`; `pages/svg/README.md` says which version they are.
