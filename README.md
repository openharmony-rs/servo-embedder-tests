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

Markers: `network`, `platform("ohos")`, `servo_prefs(name=value)`, `servo_args("--flag")`.

`servo_embedder_tests.touch` turns a WebDriver element into a screen point and taps it through the OS. Use
it when the test is about the platform path: WebDriver clicks enter Servo below ArkUI.

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
