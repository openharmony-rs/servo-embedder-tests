"""Web engines that page scenarios render with: an app showing a web view, driven through WebDriver.

servoshell and ArkWeb (any app that shows `aa start -U <url>` in an ArkWeb `Web` component) are built in. Other
engines, e.g. a web view library whose engine an app switches, come from a plugin that implements the
`pytest_servo_embedder_tests_engines` hook (see `hookspecs`).
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import shlex
import shutil
import tempfile
import time
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.remote.webdriver import WebDriver

from .errors import ScenarioError
from .oracles import wait_until
from .platform import DesktopHost, OhosDevice, Platform
from .screenshots import Box
from .servoshell import Servoshell, ServoshellDesktop, ServoshellOhos
from .webdriver import connect

# Makes every ArkWeb web view open a DevTools socket, without the app calling `setWebDebuggingAccess`.
DEVTOOLS_PARAM = "web.debug.devtools"
DEVTOOLS_SOCKET = "webview_devtools_remote_{pid}"
CHROMEDRIVER_URL = "https://storage.googleapis.com/chrome-for-testing-public/{version}/linux64/chromedriver-linux64.zip"


class Engine(ABC):
    """An app that shows pages in a web view and gives a WebDriver session to it."""

    name: str

    @abstractmethod
    def open(self, url: str) -> WebDriver:
        """Starts the app if it isn't running, brings it to the foreground and shows `url`."""

    @abstractmethod
    def screen_box(self) -> Box:
        """Where the web view is on the screen, in screen pixels."""

    @abstractmethod
    def describe(self) -> dict[str, object]:
        """What renders: engine and app versions, user agent. Stored with the captures."""

    @abstractmethod
    def is_running(self) -> bool: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def platform(self) -> Platform: ...

    @property
    @abstractmethod
    def driver(self) -> WebDriver: ...

    def screenshot(self, dest: Path) -> Path:
        """A PNG of the web view as it is on the screen; on desktop, WebDriver's screenshot of the viewport."""
        platform = self.platform
        if isinstance(platform, DesktopHost):
            dest.write_bytes(self.driver.get_screenshot_as_png())
            return dest
        with tempfile.TemporaryDirectory() as directory:
            screen = platform.screenshot(Path(directory) / "screen.png")
            with Image.open(screen) as image:
                image.convert("RGB").crop(self.screen_box()).save(dest)
        return dest


def _foreground(device: OhosDevice, pid: int, timeout: float = 10) -> None:
    """Waits for the app `pid` to come to the front after `aa start`, then dismisses what keeps it from it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if device.has_focus(pid):
            return
        time.sleep(0.3)
    if not device.ensure_focused(pid):
        window = device.focused_window()
        raise ScenarioError(f"pid {pid} is not in the foreground (focused window: {window.name if window else None})")


class ServoshellEngine(Engine):
    """servoshell, with its WebDriver server."""

    name = "servoshell"

    def __init__(self, platform: Platform, app: Servoshell, args: Sequence[str] = ()) -> None:
        self._platform = platform
        self.app = app
        self.args = list(args)
        self.port = random.randrange(9901, 9999)
        self._driver: WebDriver | None = None
        self._box: Box | None = None
        self._forwarded = False

    @classmethod
    def create(cls, platform: Platform, bundle: str, binary: Path, log: Path, args: Sequence[str]) -> ServoshellEngine:
        if isinstance(platform, OhosDevice):
            return cls(platform, ServoshellOhos(platform, bundle), args)
        return cls(platform, ServoshellDesktop(platform, binary, log), args)

    @property
    def platform(self) -> Platform:
        return self._platform

    @property
    def driver(self) -> WebDriver:
        if self._driver is None:
            raise ScenarioError("servoshell is not running")
        return self._driver

    def open(self, url: str) -> WebDriver:
        if self._driver is None or not self.is_running():
            self.stop()
            if not self._forwarded:
                self._platform.forward(self.port)
                self._forwarded = True
            self.app.start("about:blank", self.port, {}, self.args)
            self._driver = connect(self.port, alive=self.app.is_running)
        elif isinstance(self.app, ServoshellOhos) and self.app.pid is not None:
            assert isinstance(self._platform, OhosDevice)
            _foreground(self._platform, self.app.pid)
        self._driver.get(url)
        return self._driver

    def screen_box(self) -> Box:
        if self._box is None:
            assert isinstance(self._platform, OhosDevice)
            webview = self._platform.dump_layout().webview()
            if webview is None:
                raise ScenarioError("servoshell shows no XComponent")
            self._box = webview.bounds
        return self._box

    def describe(self) -> dict[str, object]:
        info: dict[str, object] = {
            "engine": self.name,
            "user_agent": self.driver.execute_script("return navigator.userAgent"),
        }
        if isinstance(self.app, ServoshellOhos):
            info["bundle"] = self.app.bundle
        return info

    def is_running(self) -> bool:
        return self.app.is_running()

    def stop(self) -> None:
        if self._driver is not None:
            with contextlib.suppress(Exception):
                self._driver.quit()
            self._driver = None
        self.app.stop()
        self._box = None


class ArkWebEngine(Engine):
    """An app that shows the URL it is started with (`aa start -U`) in an ArkWeb `Web` component, driven by
    chromedriver through ArkWeb's DevTools socket. chromedriver's version must match ArkWeb's Chromium."""

    name = "arkweb"

    def __init__(
        self,
        device: OhosDevice,
        bundle: str,
        chromedriver: Path | None,
        log: Path,
        ability: str = "EntryAbility",
        parameters: Sequence[Sequence[str]] = (),
    ) -> None:
        """`parameters` are name-value pairs to start the app with, as `aa start --ps name value`."""
        self.device = device
        self.bundle = bundle
        self.chromedriver = chromedriver
        self.log = log
        self.ability = ability
        self.parameters = [(name, value) for name, value in parameters]
        self.port = random.randrange(9301, 9399)
        self.pid: int | None = None
        self._driver: WebDriver | None = None
        self._forward: tuple[str, str] | None = None
        self._box: Box | None = None

    @property
    def platform(self) -> Platform:
        return self.device

    @property
    def driver(self) -> WebDriver:
        if self._driver is None:
            raise ScenarioError(f"{self.bundle} is not running")
        return self._driver

    def _start_app(self, url: str) -> None:
        parts = ["aa", "start", "-a", self.ability, "-b", self.bundle, "-U", url]
        parts += [part for name, value in self.parameters for part in ("--ps", name, value)]
        output = self.device.shell(shlex.join(parts))
        if "successfully" not in output:
            raise ScenarioError(f"`aa start` failed: {output.strip()}")

    def open(self, url: str) -> WebDriver:
        if self._driver is not None and self.is_running():
            self._start_app(url)
            assert self.pid is not None
            _foreground(self.device, self.pid)
            self._driver.get(url)
            return self._driver
        self.stop()
        if self.device.param(DEVTOOLS_PARAM) != "true":
            self.device.shell(f"param set {DEVTOOLS_PARAM} true")
        self._start_app(url)
        self.pid = wait_until(lambda: self.device.pid_of(self.bundle), timeout=15, message=f"{self.bundle} to start")
        _foreground(self.device, self.pid)
        socket = DEVTOOLS_SOCKET.format(pid=self.pid)
        try:
            wait_until(lambda: f"@{socket}" in self.device.shell("cat /proc/net/unix"), timeout=15)
        except Exception:
            raise ScenarioError(
                f"{self.bundle} has no DevTools socket {socket}: it shows no ArkWeb web view, or ArkWeb runs another "
                "engine (on boards with Servo's ArkWeb backend, `param set web.engine.enforce 3` selects Chromium)"
            ) from None
        self._forward = (f"tcp:{self.port}", f"localabstract:{socket}")
        self.device.device.forward_port(*self._forward)
        self._driver = self._attach()
        self._driver.get(url)
        return self._driver

    def chromium_version(self) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=10) as response:
            browser = json.load(response).get("Browser", "")
        return browser.split("/", 1)[-1]

    def _attach(self) -> WebDriver:
        version = self.chromium_version()
        executable = self.chromedriver or (Path(found) if (found := shutil.which("chromedriver")) else None)
        hint = f"get chromedriver {version} from {CHROMEDRIVER_URL.format(version=version)} and pass --chromedriver"
        if executable is None:
            raise ScenarioError(f"no chromedriver for ArkWeb's Chromium {version}: {hint}")
        options = webdriver.ChromeOptions()
        options.debugger_address = f"127.0.0.1:{self.port}"
        try:
            return webdriver.Chrome(service=Service(str(executable), log_output=str(self.log)), options=options)
        except SessionNotCreatedException as error:
            message = f"chromedriver {executable} can't attach to Chromium {version} ({error.msg}): {hint}"
            raise ScenarioError(message) from None

    def screen_box(self) -> Box:
        if self._box is None:
            webs = self.device.dump_layout().find_all(type="Web")
            if not webs:
                raise ScenarioError(f"{self.bundle} shows no Web component")
            self._box = webs[-1].bounds
        return self._box

    def describe(self) -> dict[str, object]:
        user_agent = str(self.driver.execute_script("return navigator.userAgent"))
        arkweb = re.search(r"ArkWeb/(\S+)", user_agent)
        return {
            "engine": self.name,
            "bundle": self.bundle,
            "user_agent": user_agent,
            "arkweb": arkweb.group(1) if arkweb else None,
            "chromium": self.driver.capabilities.get("browserVersion"),
        }

    def is_running(self) -> bool:
        return self.pid is not None and self.device.pid_of(self.bundle) == self.pid

    def stop(self) -> None:
        if self._driver is not None:
            with contextlib.suppress(Exception):
                self._driver.quit()
            self._driver = None
        if self._forward is not None:
            with contextlib.suppress(Exception):
                self.device.device.remove_port_forward(*self._forward)
            self._forward = None
        self.device.shell(f"aa force-stop {shlex.quote(self.bundle)}")
        self.pid = None
        self._box = None
