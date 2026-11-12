"""Apps that show their pages in the system web view, driven through the UI, the same way under any web engine.

The checks only use what the platform shows: the ArkUI component tree (tabs, web views and their URLs, native
pages), screenshots, the process and the system's fault events. A web engine's accessibility tree would tell more
about a page, but only where the engine provides one. The inputs are taps at positions in the page and drags that end
without a fling, so runs on one device get the same inputs whatever the engine.

Each test's artifacts folder gets a record to review, by a person or an agent: steps.json lists every step's
screenshot and layout with what it should show and what the checks measured, overview.jpeg shows the steps side by
side, and faults.json the fault events during the test. Steps are named the same in every run, so that two runs, for
example with two web engines, can be compared step by step.
"""

from __future__ import annotations

import json
import shlex
import shutil
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import screenshots
from .errors import AppCrashed, ScenarioError, ScenarioTimeout
from .oracles import wait_until
from .platform import OhosDevice
from .ui import Layout, Node

# The dialog some apps show on their first start to accept their privacy statement and user agreement.
CONSENT_BUTTON = "Paf.Permission.button_confirm"
# Mean luminance difference (0-255) under which two screenshots count as the same screen. Blinking cursors, rotating
# search hints and small animations stay under it.
SETTLED = 1.5
# How long the screen must stay the same to count as settled, so that a page loading behind a spinner doesn't.
SETTLED_FOR = 2.0
# Contrast (0-255) a page must exceed not to count as blank. One flat colour has 0, a loading skeleton 12, the emptiest
# pages seen 20.
MIN_CONTRAST = 15
# Fault events that mean a process crashed. Web engines may render in processes of their own, named after the bundle.
CRASH_EVENTS = ("CPP_CRASH", "JS_ERROR", "RUST_PANIC", "APP_FREEZE")


@dataclass(frozen=True)
class Screen:
    """A page as the layout shows it: the page in front is a web view whose URL ends with `url` ("" for any web view),
    the selected tab has the label `tab`, native components show `texts`, and a tab bar shows or not (`tab_bar`)."""

    url: str | None = None
    tab: str | None = None
    texts: tuple[str, ...] = ()
    tab_bar: bool | None = None

    def matches(self, layout: Layout) -> bool:
        if self.url is not None:
            area = content_area(layout)
            if area is None or area.type != "Web" or not area.text.endswith(self.url):
                return False
        bar = layout.find(type="TabBar")
        if self.tab_bar is not None and (bar is not None) != self.tab_bar:
            return False
        if self.tab is not None and not (
            bar and any(item.is_selected and _label(item) == self.tab for item in bar.children)
        ):
            return False
        shown = {node.text for node in native(layout.root) if node.is_visible}
        return all(text in shown for text in self.texts)


def native(node: Node) -> Iterator[Node]:
    """`node` and its descendants, without what web views show, which depends on the web engine."""
    yield node
    if node.type != "Web":
        for child in node.children:
            yield from native(child)


def _label(item: Node) -> str | None:
    return next((node.text for node in native(item) if node.type == "Text" and node.text), None)


@dataclass(frozen=True)
class Point:
    """A position in a page's content area, as fractions of its width and height."""

    x: float
    y: float


@dataclass(frozen=True)
class Capture:
    """A step's screenshot and layout, and the content area the checks look at."""

    name: str
    screenshot: Path
    layout: Layout
    box: screenshots.Box


def content_area(layout: Layout) -> Node | None:
    """The part of the screen that shows the page in front: its web view, or the topmost native page."""
    if webs := layout.find_all(type="Web"):
        return webs[-1]
    pages = layout.find_all(type="NavDestination")
    return pages[-1] if pages else None


class WebViewApp:
    """An app on an hdc device whose pages are system web views, recording the steps of a test in `artifacts`."""

    def __init__(self, device: OhosDevice, bundle: str, artifacts: Path, ability: str = "EntryAbility") -> None:
        self.device = device
        self.bundle = bundle
        self.ability = ability
        self.artifacts = artifacts
        self.pid: int | None = None
        self.started_ms = 0
        self.version = device.bundle_version(bundle)
        self.steps: list[dict[str, Any]] = []
        self._count = 0
        self._last: Capture | None = None

    def start(self, screen: Screen, url: str | None = None, parameters: Sequence[tuple[str, str]] = ()) -> Layout:
        """Cold-starts the app with `url` and `--ps` `parameters` if given, accepts its consent dialog if it shows
        one, and waits for `screen`."""
        self.stop()
        self.started_ms = self.device.time_ms() - 1000
        command = ["aa", "start", "-a", self.ability, "-b", self.bundle, *(["-U", url] if url else [])]
        command += [part for name, value in parameters for part in ("--ps", name, value)]
        output = self.device.shell(shlex.join(command))
        if "successfully" not in output:
            raise ScenarioError(f"`aa start` failed: {output.strip()}")
        pid = self.pid = wait_until(lambda: self.device.pid_of(self.bundle), message=f"a {self.bundle} process")
        try:
            wait_until(lambda: self.device.has_focus(pid), timeout=10, message=f"{self.bundle} to have focus")
        except ScenarioTimeout:
            # Something is in front, e.g. the lock screen or the USB mode pop-up.
            if not self.device.ensure_focused(pid):
                raise ScenarioError(f"{self.bundle} is not in the foreground") from None

        def ready() -> Layout | None:
            layout = self.device.dump_layout()
            if consent := layout.find(id=CONSENT_BUTTON):
                self.device.tap(*consent.center)
                return None
            if screen.matches(layout):
                return layout
            self.check_running()
            return None

        return wait_until(ready, timeout=30, interval=0, message=f"{self.bundle} to show {screen}")

    def stop(self) -> None:
        self.device.shell(f"aa force-stop {shlex.quote(self.bundle)}")
        self.pid = None

    def is_running(self) -> bool:
        return self.pid is not None and self.device.pid_of(self.bundle) == self.pid

    def check_running(self) -> None:
        if not self.is_running():
            raise AppCrashed(f"{self.bundle} exited")

    def crashes(self) -> list[dict[str, object]]:
        """Crash events of the app's processes since it started."""
        return [
            event
            for event in self.device.faults(self.started_ms)
            if event.get("name_") in CRASH_EVENTS and str(event.get("MODULE", "")).startswith(self.bundle)
        ]

    def check_crashes(self) -> None:
        if crashes := self.crashes():
            summary = ", ".join(f"{event.get('name_')} in {event.get('MODULE')}" for event in crashes)
            raise AppCrashed(f"{summary}; see faults.json")

    def wait_for(self, *screens: Screen, timeout: float = 20, dump: Path | None = None) -> tuple[Screen, Layout]:
        """Waits until the layout shows one of `screens` and returns it; the last layout dump is kept at `dump`."""

        def matching() -> tuple[Screen, Layout] | None:
            layout = self.device.dump_layout(dump)
            if shown := next((screen for screen in screens if screen.matches(layout)), None):
                return shown, layout
            self.check_running()
            return None

        return wait_until(matching, timeout=timeout, interval=0, message=" or ".join(map(str, screens)))

    def tap_tab(self, label: str) -> None:
        bar = self.device.dump_layout().find(type="TabBar")
        item = next((item for item in bar.children if _label(item) == label), None) if bar else None
        if item is None:
            raise ScenarioError(f"no {label!r} tab in the tab bar")
        self.device.tap(*item.center)

    def tap(self, point: Point) -> None:
        """Taps `point` in the content area of the last captured step."""
        left, top, right, bottom = self._content_box()
        self.device.tap(round(left + (right - left) * point.x), round(top + (bottom - top) * point.y))

    def drag(self, fraction: float) -> int:
        """Drags the last captured page up by `fraction` of its content area's height (down if negative), without a
        fling. Returns the distance in pixels."""
        left, top, right, bottom = self._content_box()
        x, y, half = (left + right) // 2, (top + bottom) // 2, round((bottom - top) * fraction / 2)
        self.device.drag(x, y + half, x, y - half)
        return abs(2 * half)

    def back(self) -> None:
        self.device.key("Back")

    def capture(
        self, name: str, screen: Screen, expect: str, interstitial: Screen | None = None, timeout: float = 45
    ) -> Capture:
        """Waits for `screen` and for the screen to settle, and keeps a screenshot and the layout as the next step, with
        `expect`, what it should show. An `interstitial` page that shows up in front, such as a login page, is kept as
        a step of its own and closed with Back."""
        self._count += 1
        stem = f"{self._count:02d}-{name}"
        dump, path = self.artifacts / f"{stem}.json", self.artifacts / f"{stem}.jpeg"
        screens = (screen,) if interstitial is None else (screen, interstitial)
        closed = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            shown, layout = self.wait_for(*screens, dump=dump)
            if shown is interstitial:
                if closed:
                    raise AssertionError(f"{stem}: {interstitial} came back after Back")
                self._keep(f"{stem}-interstitial", interstitial, f"{interstitial}, which the test closes with Back")
                self.back()
                wait_until(
                    lambda: not interstitial.matches(self.device.dump_layout()), message=f"Back to close {interstitial}"
                )
                closed = True
                continue
            area = content_area(layout)
            box = area.bounds if area is not None else layout.root.bounds
            self._settle(path, box)
            # The page in front may have changed meanwhile.
            layout = self.device.dump_layout(dump)
            area = content_area(layout)
            if screen.matches(layout) and area is not None and area.bounds == box:
                self._last = Capture(stem, path, layout, box)
                self._add_step(self._last, expect, area)
                return self._last
            path.unlink(missing_ok=True)
        raise ScenarioTimeout(f"{stem}: the screen did not settle on {screen}")

    def note(self, capture: Capture, **measured: object) -> None:
        """Adds what a check measured on `capture` to its step in steps.json."""
        step = next(step for step in self.steps if step["name"] == capture.name)
        step["measured"].update(measured)
        self._write_record()

    def save_failure(self) -> None:
        """Keeps a screenshot and the layout of the screen as a failed step left it."""
        for name, step in (("failure.jpeg", self.device.screenshot), ("failure.json", self.device.dump_layout)):
            try:
                step(self.artifacts / name)
            except Exception as error:
                with (self.artifacts / "errors.txt").open("a", encoding="utf-8") as f:
                    f.write(f"{name}: {error}\n")

    def finish(self) -> None:
        """Writes the overview of the steps and the fault events since the start."""
        shots = [(str(step["name"]), self.artifacts / str(step["screenshot"])) for step in self.steps]
        if (failure := self.artifacts / "failure.jpeg").is_file():
            shots.append(("failure", failure))
        if shots:
            screenshots.contact_sheet(shots, self.artifacts / "overview.jpeg")
        if self.started_ms and (faults := self.device.faults(self.started_ms)):
            (self.artifacts / "faults.json").write_text(
                json.dumps(faults, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )

    def _keep(self, stem: str, screen: Screen, expect: str) -> None:
        """Keeps the screen as a step, without waiting for it to settle: `screen` is only passing through."""
        dump, path = self.artifacts / f"{stem}.json", self.artifacts / f"{stem}.jpeg"
        self.device.screenshot(path)
        layout = self.device.dump_layout(dump)
        area = content_area(layout)
        self._add_step(Capture(stem, path, layout, area.bounds if area else layout.root.bounds), expect, area)

    def _add_step(self, capture: Capture, expect: str, area: Node | None) -> None:
        self.steps.append(
            {
                "name": capture.name,
                "screenshot": capture.screenshot.name,
                "layout": capture.screenshot.with_suffix(".json").name,
                "box": capture.box,
                "url": area.text if area is not None and area.type == "Web" else None,
                "expect": expect,
                "measured": {"contrast": round(screenshots.contrast(capture.screenshot, capture.box), 1)},
            }
        )
        self._write_record()

    def _write_record(self) -> None:
        record = {"bundle": self.bundle, "version": self.version, "steps": self.steps}
        (self.artifacts / "steps.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _settle(self, dest: Path, box: screenshots.Box, timeout: float = 20) -> None:
        """Takes screenshots until `box` shows the same for SETTLED_FOR seconds and isn't blank, such as a loading
        skeleton, and keeps the last at `dest`. Every change starts the time again, so that carousels can turn."""
        with tempfile.TemporaryDirectory() as directory:
            reference, current = Path(directory) / "reference.jpeg", Path(directory) / "current.jpeg"
            since: float | None = None

            def settled() -> bool:
                nonlocal since
                self.check_running()
                self.device.screenshot(current)
                if since is None or screenshots.difference(reference, current, box) >= SETTLED:
                    shutil.copyfile(current, reference)
                    since = time.monotonic()
                    return False
                return time.monotonic() - since >= SETTLED_FOR and screenshots.contrast(current, box) > MIN_CONTRAST

            try:
                wait_until(
                    settled, timeout=timeout, interval=0, message=f"the screen to stay the same {SETTLED_FOR:g} s"
                )
            except ScenarioTimeout:
                if current.is_file() and (contrast := screenshots.contrast(current, box)) <= MIN_CONTRAST:
                    raise AssertionError(f"{dest.name}: the page stayed blank (contrast {contrast:.1f})") from None
                raise
            finally:
                if current.is_file():
                    shutil.copyfile(current, dest)

    def _content_box(self) -> screenshots.Box:
        if self._last is None:
            raise ScenarioError("no step captured yet to tap or drag in")
        return self._last.box
