"""pytest plugin: options, fixtures, expectations and artifacts for servoshell scenarios."""

from __future__ import annotations

import contextlib
import os
import random
import re
import shutil
import signal
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import pytest
from ohos_test_ime import TestIme
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.remote.webdriver import WebDriver

from .engines import ArkWebEngine, Engine, ServoshellEngine
from .errors import AppCrashed, ScenarioError, ScenarioTimeout
from .expectations import Expectations, Result, RunReport
from .page_scenarios import PageRuns, Renderer
from .pages import PageServer
from .platform import DesktopHost, HilogStream, OhosDevice, Platform
from .proxy import MitmProxy, ProxyMode
from .references import ArkWebViewer, References, ReferenceStore
from .servoshell import PrefValue, Servoshell, ServoshellDesktop, ServoshellOhos
from .webdriver import connect
from .webview_app import WebViewApp

BUNDLED_SCENARIOS = Path(str(resources.files("servo_embedder_tests") / "scenarios"))
EXPECTED_KEY = pytest.StashKey[list[str]]()
# Marks a per-test artifacts folder, so that the next run can remove it.
TEST_ARTIFACTS_MARKER = "test-key.txt"
RUN_ARTIFACTS = ("results.json", "hilog.txt", "servoshell.log", "mitmproxy.log", "chromedriver.log", "report.html")
TEST_KEY = pytest.StashKey[str]()
NEEDS_TEST_IME = "needs a test IME: pass --test-ime <bundle>"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("servo-embedder-tests")
    group.addoption(
        "--platform", choices=("ohos", "desktop"), help="where servoshell runs; enables the plugin (`run` adds ohos)"
    )
    group.addoption("--hdc-target", default=os.environ.get("HDC_TARGET"), help="hdc target (required with >1 device)")
    group.addoption("--bundle", default="org.servo.servo", help="bundle name of servoshell on OHOS")
    group.addoption("--hap", type=Path, help="install this HAP before the run")
    group.addoption("--servo-bin", type=Path, default=Path("target/release/servoshell"), help="desktop servoshell")
    group.addoption("--servo-arg", action="append", default=[], help="extra servoshell argument (repeatable)")
    group.addoption("--device-class", help="expectations device class (default: detected)")
    group.addoption("--expectations", type=Path, help="expectations TOML file")
    group.addoption("--results-json", type=Path, help="write per-test results here (default: <artifacts>/results.json)")
    group.addoption(
        "--artifacts", type=Path, default=Path("servo-embedder-tests-artifacts"), help="artifacts directory"
    )
    group.addoption("--proxy", type=ProxyMode, choices=list(ProxyMode), default=ProxyMode.OFF, help="mitmproxy mode")
    group.addoption("--proxy-dump", type=Path, default=Path("/tmp/mitmproxy-dump"), help="mitmproxy dump file")
    group.addoption("--pages-dir", type=Path, action="append", default=[], help="extra fixture page directory")
    group.addoption(
        "--webview-apps",
        type=Path,
        action="append",
        default=[],
        help="TOML file or folder of app definitions for the webview_apps scenarios (repeatable)",
    )
    group.addoption("--test-ime", metavar="BUNDLE", help="bundle of an installed test IME (enables IME scenarios)")
    group.addoption("--test-ime-hap", type=Path, help="install this test IME HAP before the run")
    group.addoption("--test-ime-port", type=int, default=8971, help="the test IME's control port on the device")
    group.addoption("--scenario-timeout", type=float, default=300, help="per-test timeout in seconds (0: none)")
    group.addoption(
        "--arkweb-viewer",
        metavar="BUNDLE",
        help="installed app that shows `aa start -U <url>` with ArkWeb; captures the references --references lacks",
    )
    group.addoption("--arkweb-viewer-ability", default="EntryAbility", help="the --arkweb-viewer ability to start")
    group.addoption(
        "--arkweb-viewer-ps",
        nargs=2,
        action="append",
        default=[],
        metavar=("NAME", "VALUE"),
        help="start --arkweb-viewer with this `aa start --ps` parameter (repeatable)",
    )
    group.addoption("--references", type=Path, help="reference renderings, in a folder per device (--reference-key)")
    group.addoption(
        "--reference-key", help="the device's folder in --references and --baselines (default: the device's name)"
    )
    group.addoption(
        "--page-scenarios", type=Path, action="append", default=[], help="folder with a subfolder per page scenario"
    )
    group.addoption("--engine", default="servoshell", help="engine that renders the page scenarios")
    group.addoption("--reference-engine", help="engine whose renderings the page scenarios are compared with")
    group.addoption(
        "--engine-version",
        metavar="NAME",
        help="a name for the build of --engine, e.g. a commit: kept with its captures and baselines, and reported",
    )
    group.addoption("--reference-engine-version", metavar="NAME", help="the same for --reference-engine")
    group.addoption("--baselines", type=Path, help="stored captures of page scenarios, per device and engine")
    group.addoption("--update-baselines", action="store_true", help="store this run's captures as the baselines")
    group.addoption(
        "--update-references", action="store_true", help="render the references anew even where they are stored"
    )
    group.addoption("--chromedriver", type=Path, help="chromedriver matching ArkWeb's Chromium, for the arkweb engine")


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    from . import hookspecs

    pluginmanager.add_hookspecs(hookspecs)


def pytest_configure(config: pytest.Config) -> None:
    for marker in (
        "network: loads real sites; uses --proxy",
        "servo_prefs(**prefs): Servo preferences for this test's servoshell",
        "servo_args(*args): extra servoshell arguments for this test",
        "platform(*families): run only on these platform families (ohos, desktop)",
        "explicit: run only when the command line names the test's file or a folder it is in",
    ):
        config.addinivalue_line("markers", marker)
    if getattr(config.option, "platform", None) is None or config.option.collectonly:
        return
    config.stash[RUN_KEY] = ScenarioRun(config)


@dataclass
class ScenarioRun:
    config: pytest.Config
    _platform: Platform | None = None
    _device_class: str | None = None
    expectations: Expectations = field(init=False)
    report: RunReport = field(init=False)
    stack: contextlib.ExitStack = field(default_factory=contextlib.ExitStack)
    page_report: Path | None = None

    def __post_init__(self) -> None:
        self.expectations = Expectations.load(self.config.option.expectations)
        self.report = RunReport(self.config.option.device_class or "unknown")
        self.artifacts: Path = self.config.option.artifacts
        self.webdriver_port = random.randrange(9001, 9900)

    @property
    def family(self) -> str:
        return self.config.option.platform

    @property
    def platform(self) -> Platform:
        if self._platform is None:
            if self.family == "ohos":
                self._platform = OhosDevice(self.config.option.hdc_target)
            else:
                self._platform = DesktopHost()
        return self._platform

    @property
    def device_class(self) -> str:
        if self._device_class is None:
            self._device_class = self.config.option.device_class or self.platform.device_class()
            self.report.device_class = self._device_class
        return self._device_class


RUN_KEY = pytest.StashKey[ScenarioRun]()


def _run(config: pytest.Config) -> ScenarioRun:
    return config.stash[RUN_KEY]


def test_key(item: pytest.Item) -> str:
    path = Path(item.path)
    try:
        relative = path.relative_to(BUNDLED_SCENARIOS)
    except ValueError:
        try:
            relative = path.relative_to(item.config.rootpath)
        except ValueError:
            relative = Path(path.name)
    name = item.nodeid.split("::", 1)[1] if "::" in item.nodeid else item.name
    if "\\" in name:
        # pytest writes non-ASCII characters of parameters as escapes (`具`): keys name scenarios as they are.
        name = name.encode("ascii", "backslashreplace").decode("unicode_escape")
    return f"{relative.as_posix()}::{name}"


def pytest_report_header(config: pytest.Config) -> list[str]:
    if RUN_KEY not in config.stash:
        return []
    run = _run(config)
    lines = [f"servo-embedder-tests: platform {run.family}, expectations {config.option.expectations or '(none)'}"]
    with contextlib.suppress(Exception):
        lines.append(
            f"servo-embedder-tests: device {run.platform!r}, class {run.device_class}, {run.platform.describe()}"
        )
    return lines


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if getattr(config.option, "platform", None) is not None:
        deselected = [item for item in items if item.get_closest_marker("explicit") and not _named(item, config)]
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if item not in deselected]
    if RUN_KEY not in config.stash:
        return
    run = _run(config)
    for item in items:
        key = test_key(item)
        item.stash[TEST_KEY] = key
        families = [f for m in item.iter_markers("platform") for f in m.args]
        if families and run.family not in families:
            item.add_marker(pytest.mark.skip(reason=f"only on {', '.join(families)}"))
            continue
        if "ime" in getattr(item, "fixturenames", ()) and not config.option.test_ime:
            # The fixture would skip only after `servo` has started servoshell.
            item.add_marker(pytest.mark.skip(reason=NEEDS_TEST_IME))
            continue
        expected = run.expectations.get(key, run.device_class)
        item.stash[EXPECTED_KEY] = expected.statuses
        if expected.statuses == ["SKIP"]:
            item.add_marker(pytest.mark.skip(reason=f"expectations: SKIP {expected.issue or ''}".strip()))


def pytest_sessionstart(session: pytest.Session) -> None:
    config = session.config
    if RUN_KEY not in config.stash:
        return
    run = _run(config)
    run.artifacts.mkdir(parents=True, exist_ok=True)
    _clear_artifacts(run.artifacts)
    if config.option.results_json:
        config.option.results_json.unlink(missing_ok=True)
    if run.family == "ohos":
        device = run.platform
        assert isinstance(device, OhosDevice)
        if config.option.hap:
            device.install(config.option.hap, config.option.bundle)
        if config.option.test_ime_hap:
            device.install(config.option.test_ime_hap, config.option.test_ime or config.option.bundle)
        run.report.environment.update(device.describe())


def _named(item: pytest.Item, config: pytest.Config) -> bool:
    """Whether the command line names the item's file, or a folder it is in other than the bundled scenarios."""
    path = Path(item.path).resolve()
    for arg in config.args:
        named = (config.invocation_params.dir / arg.split("::", 1)[0]).resolve()
        if named != BUNDLED_SCENARIOS.resolve() and (named == path or named in path.parents):
            return True
    return False


def pytest_collection_finish(session: pytest.Session) -> None:
    config = session.config
    if RUN_KEY not in config.stash or not any("servo" in getattr(item, "fixturenames", ()) for item in session.items):
        return
    device = _run(config).platform
    if isinstance(device, OhosDevice) and not device.is_installed(config.option.bundle):
        raise pytest.UsageError(f"{config.option.bundle} is not installed on {device.target}; pass --hap")


def artifacts_dir(run: ScenarioRun, key: str) -> Path:
    """The artifacts folder of the test `key`, created and marked for removal by the next run."""
    # Non-ASCII characters as escapes, so that keys that differ only in them get folders of their own.
    ascii_key = key.encode("ascii", "backslashreplace").decode("ascii")
    directory = run.artifacts / re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_key)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / TEST_ARTIFACTS_MARKER).write_text(key + "\n", encoding="utf-8")
    return directory


def _clear_artifacts(directory: Path) -> None:
    """Removes what an earlier run left in `directory`, and nothing else."""
    for name in RUN_ARTIFACTS:
        (directory / name).unlink(missing_ok=True)
    for marker in directory.glob(f"*/{TEST_ARTIFACTS_MARKER}"):
        shutil.rmtree(marker.parent)


def pytest_sessionfinish(session: pytest.Session) -> None:
    config = session.config
    if RUN_KEY not in config.stash:
        return
    run = _run(config)
    path = config.option.results_json or run.artifacts / "results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.report.to_json())
    from .report import write_report

    run.page_report = write_report(run.artifacts, path)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(_markdown_summary(run.report))
    run.stack.close()
    if run._platform is not None:
        run.platform.remove_forwards()


def _markdown_summary(report: RunReport) -> str:
    unexpected = [r for r in report.results.values() if r.unexpected]
    lines = [f"### servo-embedder-tests ({report.device_class})", ""]
    counts: dict[str, int] = {}
    for result in report.results.values():
        counts[result.status] = counts.get(result.status, 0) + 1
    lines.append(", ".join(f"{status}: {count}" for status, count in sorted(counts.items())) + "\n")
    if unexpected:
        lines += ["| Test | Expected | Got |", "| --- | --- | --- |"]
        lines += [f"| `{r.key}` | {'/'.join(r.expected)} | {r.status} |" for r in unexpected]
    else:
        lines.append("No unexpected results.")
    return "\n".join(lines) + "\n\n"


def _status(call: pytest.CallInfo[None], item: pytest.Item) -> str:
    if call.excinfo is None:
        return "PASS"
    if call.excinfo.errisinstance(pytest.skip.Exception):
        return "SKIP"
    if call.excinfo.errisinstance((ScenarioTimeout, TimeoutException)):
        return "TIMEOUT"
    if call.excinfo.errisinstance(AppCrashed):
        return "CRASH"
    return "FAIL"


def _is_running(app: Servoshell | WebViewApp) -> bool | None:
    """Whether `app` is running, or None if that can't be told, e.g. because the device is gone."""
    try:
        return app.is_running()
    except Exception:
        return None


def _crashed(app: WebViewApp) -> bool:
    """Whether the app exited or one of its processes crashed; False if that can't be told."""
    try:
        return not app.is_running() or bool(app.crashes())
    except Exception:
        return False


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    timeout = item.config.option.scenario_timeout if RUN_KEY in item.config.stash else 0
    if not timeout or not hasattr(signal, "SIGALRM"):
        return (yield)

    def expire(signum: int, frame: object) -> None:
        raise ScenarioTimeout(f"test exceeded --scenario-timeout={timeout}s")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return (yield)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    config = item.config
    if RUN_KEY not in config.stash or TEST_KEY not in item.stash:
        return report
    run = _run(config)
    key = item.stash[TEST_KEY]
    expected = item.stash.get(EXPECTED_KEY, ["PASS"])
    if report.when == "teardown" or (report.when == "setup" and report.passed):
        return report
    status = _status(call, item)
    funcargs = getattr(item, "funcargs", {})
    session = funcargs.get("servo")
    webview_apps = [value for value in funcargs.values() if isinstance(value, WebViewApp)]
    apps: list[Servoshell | WebViewApp] = [session.app] if isinstance(session, ServoSession) else []
    if status in ("PASS", "FAIL") and any(_is_running(app) is False for app in apps + webview_apps):
        status = "CRASH"
    elif status in ("FAIL", "TIMEOUT") and any(_crashed(app) for app in webview_apps):
        status = "CRASH"
    if status == "SKIP":
        run.report.results[key] = Result(key, "SKIP", expected, str(call.excinfo.value) if call.excinfo else "")
        return report
    message = call.excinfo.exconly() if call.excinfo else ""
    result = Result(key, status, expected, message, round(call.duration, 2))
    run.report.results[key] = result
    if not result.unexpected:
        if status != "PASS":
            report.outcome = "skipped"
            report.wasxfail = f"expected {status}"  # type: ignore[attr-defined]
        return report
    if status == "PASS":
        report.outcome = "failed"
        report.longrepr = f"Unexpected PASS (expected {'/'.join(expected)}): update the expectations"
    elif status == "CRASH" and report.passed:
        report.outcome = "failed"
        report.longrepr = "the app exited during the test"
    if isinstance(session, ServoSession):
        session.save_artifacts(key)
    for app in webview_apps:
        app.save_failure()
    return report


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    if RUN_KEY not in config.stash:
        return
    run = _run(config)
    unexpected = [r for r in run.report.results.values() if r.unexpected]
    terminalreporter.section("servo-embedder-tests")
    terminalreporter.line(f"device class: {run.report.device_class}; {len(run.report.results)} results")
    for result in unexpected:
        terminalreporter.line(f"UNEXPECTED {result.status} (expected {'/'.join(result.expected)}): {result.key}")
    if not unexpected:
        terminalreporter.line("no unexpected results")
    if run.page_report is not None:
        terminalreporter.line(f"page scenario report: {run.page_report}")


@pytest.fixture(scope="session")
def scenario_run(pytestconfig: pytest.Config) -> ScenarioRun:
    if RUN_KEY not in pytestconfig.stash:
        pytest.skip("servo-embedder-tests is not configured")
    return _run(pytestconfig)


@pytest.fixture(scope="session")
def platform(scenario_run: ScenarioRun) -> Iterator[Platform]:
    platform = scenario_run.platform
    with platform.awake():
        yield platform


@pytest.fixture(scope="session")
def device(platform: Platform) -> OhosDevice:
    if not isinstance(platform, OhosDevice):
        pytest.skip("needs an OHOS device")
    return platform


@pytest.fixture(scope="session")
def hilog(device: OhosDevice, scenario_run: ScenarioRun) -> Iterator[HilogStream]:
    stream = HilogStream(device, scenario_run.artifacts / "hilog.txt").start()
    yield stream
    stream.stop()


@pytest.fixture(scope="session")
def pages(platform: Platform, scenario_run: ScenarioRun) -> Iterator[PageServer]:
    options = scenario_run.config.option
    server = PageServer([*options.pages_dir, *options.page_scenarios]).start()
    platform.reverse(server.port)
    yield server
    server.stop()


@pytest.fixture(scope="session")
def proxy(platform: Platform, scenario_run: ScenarioRun) -> Iterator[MitmProxy | None]:
    mode = scenario_run.config.option.proxy
    if mode == ProxyMode.OFF:
        yield None
        return
    dump = scenario_run.config.option.proxy_dump
    if mode == ProxyMode.REPLAY and not dump.is_file():
        pytest.skip(f"no mitmproxy dump at {dump}; record one with `servo-embedder-tests record-dump`")
    mitm = MitmProxy(mode, dump, scenario_run.artifacts / "mitmproxy.log").start()
    platform.reverse(mitm.port)
    yield mitm
    mitm.stop()


@pytest.fixture(scope="session")
def _webdriver_forward(platform: Platform, scenario_run: ScenarioRun) -> int:
    platform.forward(scenario_run.webdriver_port)
    return scenario_run.webdriver_port


class ServoSession:
    """A running servoshell with a WebDriver session."""

    def __init__(
        self,
        run: ScenarioRun,
        app: Servoshell,
        item: pytest.Item,
        hilog: HilogStream | None,
        proxy: MitmProxy | None,
    ) -> None:
        self.run = run
        self.app = app
        self.item = item
        self.hilog = hilog
        self.proxy = proxy
        self.hilog_mark = hilog.mark() if hilog else 0
        self._driver: WebDriver | None = None

    @property
    def platform(self) -> Platform:
        return self.run.platform

    @property
    def driver(self) -> WebDriver:
        if self._driver is None:
            raise ScenarioError("servoshell was started without WebDriver")
        return self._driver

    def start(
        self,
        url: str = "about:blank",
        prefs: dict[str, PrefValue] | None = None,
        args: list[str] | None = None,
        webdriver: bool = True,
    ) -> ServoSession:
        merged_prefs: dict[str, PrefValue] = {}
        merged_args = list(self.run.config.option.servo_arg)
        for marker in reversed(list(self.item.iter_markers("servo_prefs"))):
            merged_prefs.update(marker.kwargs)
        for marker in self.item.iter_markers("servo_args"):
            merged_args += list(marker.args)
        merged_prefs.update(prefs or {})
        merged_args += args or []
        if self.proxy is not None:
            proxy_prefs, proxy_args = self.proxy.servoshell_args()
            merged_prefs.update(proxy_prefs)
            merged_args += proxy_args
        self.stop()
        port = self.run.webdriver_port if webdriver else None
        self.app.start(url, port, merged_prefs, merged_args)
        if port is not None:
            self._driver = connect(port, alive=self.app.is_running)
        return self

    def stop(self) -> None:
        if self._driver is not None:
            with contextlib.suppress(Exception):
                self._driver.quit()
            self._driver = None
        self.app.stop()

    def reuse(self, item: pytest.Item) -> ServoSession:
        """Makes this the session of the test `item`, (re)starting servoshell unless it already runs."""
        self.item = item
        self.hilog_mark = self.hilog.mark() if self.hilog else 0
        if self._driver is not None and _is_running(self.app):
            return self
        try:
            self.start()
        except Exception:
            self.save_artifacts(item.stash.get(TEST_KEY, item.nodeid))
            self.stop()
            raise
        return self

    def save_artifacts(self, key: str) -> Path:
        directory = artifacts_dir(self.run, key)
        steps: list[tuple[str, object]] = []
        if isinstance(self.platform, OhosDevice):
            device = self.platform
            steps.append(("screenshot", lambda: device.screenshot(directory / "screen.jpeg")))
            steps.append(("layout", lambda: device.dump_layout(directory / "layout.json")))
        elif self._driver is not None:
            steps.append(("screenshot", lambda: self.driver.save_screenshot(str(directory / "screen.png"))))
        if self._driver is not None and _is_running(self.app):
            steps.append(("page source", lambda: (directory / "page.html").write_text(self.driver.page_source)))
        if self.hilog is not None:
            hilog = self.hilog
            steps.append(("hilog", lambda: (directory / "hilog.txt").write_text(hilog.text(self.hilog_mark))))
        for name, step in steps:
            try:
                step()  # type: ignore[operator]
            except Exception as error:
                (directory / "errors.txt").open("a").write(f"{name}: {error}\n")
        return directory


def _new_session(request: pytest.FixtureRequest, scenario_run: ScenarioRun, platform: Platform) -> ServoSession:
    proxy = request.getfixturevalue("proxy") if request.node.get_closest_marker("network") else None
    hilog = request.getfixturevalue("hilog") if isinstance(platform, OhosDevice) else None
    if isinstance(platform, OhosDevice):
        app: Servoshell = ServoshellOhos(platform, scenario_run.config.option.bundle)
    else:
        assert isinstance(platform, DesktopHost)
        app = ServoshellDesktop(
            platform, scenario_run.config.option.servo_bin, scenario_run.artifacts / "servoshell.log"
        )
    return ServoSession(scenario_run, app, request.node, hilog, proxy)


@pytest.fixture
def servo(
    request: pytest.FixtureRequest,
    scenario_run: ScenarioRun,
    platform: Platform,
    _webdriver_forward: int,
) -> Iterator[ServoSession]:
    """servoshell started on about:blank with a WebDriver session. Call `servo.start(...)` to relaunch it."""
    session = _new_session(request, scenario_run, platform)
    yield session.reuse(request.node)
    session.stop()


@pytest.fixture(scope="module")
def shared_servo(
    request: pytest.FixtureRequest,
    scenario_run: ScenarioRun,
    platform: Platform,
    _webdriver_forward: int,
) -> Iterator[ServoSession]:
    """servoshell shared by the tests of a module, which get it from a `servo` fixture of the module that returns
    `shared_servo.reuse(request.node)`. It starts with the markers of the test that starts it, so mark the module."""
    session = _new_session(request, scenario_run, platform)
    yield session
    session.stop()


@pytest.fixture
def webdriver(servo: ServoSession) -> WebDriver:
    return servo.driver


@pytest.fixture
def artifacts(request: pytest.FixtureRequest, scenario_run: ScenarioRun) -> Path:
    """This test's artifacts folder, for screenshots and other files the test keeps whatever its result."""
    return artifacts_dir(scenario_run, request.node.stash.get(TEST_KEY, request.node.nodeid))


@pytest.fixture(scope="session")
def references(scenario_run: ScenarioRun, platform: Platform, pages: PageServer) -> References:
    """Reference renderings by ArkWeb: those in the device's folder of --references, and those captured with
    --arkweb-viewer, which are added to it, or kept with the run's artifacts without --references."""
    options = scenario_run.config.option
    if options.references:
        directory = options.references / _device_key(scenario_run, platform)
    else:
        directory = artifacts_dir(scenario_run, "references")
    viewer = None
    if options.arkweb_viewer and isinstance(platform, OhosDevice):
        viewer = ArkWebViewer(platform, options.arkweb_viewer, options.arkweb_viewer_ability, options.arkweb_viewer_ps)
    return References(ReferenceStore(directory), viewer, pages)


def _device_key(run: ScenarioRun, platform: Platform) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", run.config.option.reference_key or platform.name())


def _engines(run: ScenarioRun, platform: Platform) -> dict[str, Engine]:
    options = run.config.option
    engines: dict[str, Engine] = {
        "servoshell": ServoshellEngine.create(
            platform, options.bundle, options.servo_bin, run.artifacts / "servoshell.log", options.servo_arg
        )
    }
    if options.arkweb_viewer and isinstance(platform, OhosDevice):
        engines["arkweb"] = ArkWebEngine(
            platform,
            options.arkweb_viewer,
            options.chromedriver,
            run.artifacts / "chromedriver.log",
            options.arkweb_viewer_ability,
            options.arkweb_viewer_ps,
        )
    for found in run.config.hook.pytest_servo_embedder_tests_engines(config=run.config, platform=platform):
        for engine in found:
            engines[engine.name] = engine
    return engines


@pytest.fixture(scope="session")
def page_runs(scenario_run: ScenarioRun, platform: Platform, pages: PageServer) -> Iterator[PageRuns]:
    """The renderings of the page scenarios by --engine and --reference-engine, and the stores in --baselines."""
    options = scenario_run.config.option
    runs = PageRuns(
        _engines(scenario_run, platform),
        options.engine,
        options.reference_engine,
        Renderer(pages),
        options.baselines,
        _device_key(scenario_run, platform),
        lambda name: artifacts_dir(scenario_run, f"page-scenarios/{name}"),
        options.update_baselines,
        options.update_references,
        {
            name: version
            for name, version in (
                (options.engine, options.engine_version),
                (options.reference_engine, options.reference_engine_version),
            )
            if name and version
        },
    )
    yield runs
    runs.stop()


@pytest.fixture
def ime(device: OhosDevice, scenario_run: ScenarioRun) -> Iterator[TestIme]:
    """The test IME from ohos-ime, current for the duration of the test; the previous IME is restored after."""
    bundle = scenario_run.config.option.test_ime
    if not bundle:
        pytest.skip(NEEDS_TEST_IME)
    with TestIme(device.device, bundle, device_port=scenario_run.config.option.test_ime_port) as test_ime:
        yield test_ime
