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

The `atomic_services/` scenarios drive HarmonyOS atomic services (Mossel, JD) rather than servoshell, and run only
when named: `run atomic_services/` or a file in it. They open each tab, drag its page up and back down, open a page
from a tab and go back. The services show their pages in the system web view, so the scenarios are meant to compare
web engines on one device: they only check what the platform shows (the ArkUI tree, web view URLs, screenshots,
processes and the system's crash events) and give every run the same taps and fling-free drags. Give each engine a
device class of its own, e.g. `--device-class harmonyos-servo`, which falls back to `harmonyos` expectations;
results.json records the ArkWeb version. A service is skipped unless installed, which takes opening it once from
AppGallery with a HUAWEI ID; the scenarios accept its terms dialog.

Their checks are coarse, so a run is meant to be reviewed, by a person or an agent. Each test's artifacts folder has
`overview.jpeg`, the steps side by side; `steps.json`, every step's screenshot and layout dump with what it should
show (`expect`) and what the checks measured; `faults.json`, the system's fault events during the test; and
`failure.jpeg` when a check failed. Look for blank or half-rendered pages, spinners and error pages, missing images,
broken layout or text, and screenshots that don't show what `expect` says, above all in steps with a `review` note,
which the checks couldn't judge. To compare two runs, pair the steps by test and name; the services show live
content, so feeds, campaigns and search hints differ between runs anyway.

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

Pages that load `report.js` can call `report({...})`; tests read the objects with
`pages.wait_for_report(predicate)`. That works without WebDriver, e.g. for IME scenarios, where
servoshell must run as a user would.

Markers: `network`, `platform("ohos")`, `servo_prefs(name=value)`, `servo_args("--flag")`, `explicit`.

`servo_embedder_tests.touch` turns a WebDriver element into a screen point and taps it through the OS. Use
it when the test is about the platform path: WebDriver clicks enter Servo below ArkUI.

`servo_embedder_tests.atomic_service.AtomicService` drives an app through `uitest` and `uinput`:
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
keep their notices.
