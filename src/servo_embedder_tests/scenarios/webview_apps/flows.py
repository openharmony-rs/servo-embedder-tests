"""What the web view app scenarios do: open each tab and drag its page up and back down, and open a page from a
tab, drag it, and go back. The apps, their tabs and visits are defined in TOML files (see `load`).

The checks are coarse: the page shows, isn't blank, moves the way it was dragged and comes back, and nothing crashes.
Each step in a test's steps.json says what its screenshot should show, for a reviewer to check the rest, and steps the
checks couldn't judge say why under "review".
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from servo_embedder_tests import screenshots
from servo_embedder_tests.platform import OhosDevice
from servo_embedder_tests.webview_app import Capture, Point, Screen, WebViewApp

# Drags move a third of the page's height, ~800 px on a phone, so a scrolled page still shows two thirds of what it
# showed before.
DRAG = 1 / 3
# The drag back is shorter, so that it doesn't pull past the top, which reloads some pages.
DRAG_BACK = DRAG * 3 / 4
# A page moves by at most this times the drag distance; more would be a fling.
MAX_SHIFT = 1.25
# Screen pixels a page that scrolls must move per drag; the shortest page seen moves ~500.
MIN_SCROLL = 200
# Luminance difference (0-255) a page that doesn't scroll may show after a drag.
MAX_UNCHANGED = 3
# Luminance difference allowed between a page before opening another one and after coming back. Other pages differ
# by 30 or more.
MAX_BACK_DIFFERENCE = 10


@dataclass(frozen=True)
class Tab:
    # The test id.
    name: str
    label: str
    screen: Screen
    # Whether its page scrolls. A page that doesn't is only checked not to move; what it shows is for the reviewer.
    scrolls: bool = True
    # A page the tab may open in front of its own, such as a login page, which Back closes.
    interstitial: Screen | None = None


@dataclass(frozen=True)
class Visit:
    """Opening `description`, the page `screen`, from `tab` by tapping `target`, after dragging the tab's page by
    `drag_first`."""

    name: str
    tab: Tab
    target: Point
    screen: Screen
    description: str
    drag_first: float = 0


@dataclass(frozen=True)
class App:
    # The test ids' prefix.
    name: str
    bundle: str
    # What it is called, e.g. in an app store.
    label: str
    home: Screen
    tabs: tuple[Tab, ...]
    visits: tuple[Visit, ...]
    # How to install it, for the message of tests skipped without it.
    install: str = "install it"
    # What to start it with: a page of the harness's page server (`start_page`) or a URL, passed as `-U`, and
    # `--ps` parameters.
    start_page: str | None = None
    start_url: str | None = None
    parameters: tuple[tuple[str, str], ...] = ()
    # The ability to start.
    ability: str = "EntryAbility"


def _screen(raw: dict[str, Any]) -> Screen:
    return Screen(raw.get("url"), raw.get("tab"), tuple(raw.get("texts", ())), raw.get("tab_bar"))


def _app(raw: dict[str, Any], where: str) -> App:
    """The app `raw`, a table of a definitions file, describes; see `load`."""
    try:
        tabs = {
            tab["name"]: Tab(
                tab["name"],
                tab["label"],
                Screen(url=tab.get("url", ""), tab=tab["label"]),
                tab.get("scrolls", True),
                _screen(tab["interstitial"]) if "interstitial" in tab else None,
            )
            for tab in raw.get("tab", [])
        }
        visits = tuple(
            Visit(
                visit["name"],
                tabs[visit["tab"]],
                Point(*visit["target"]),
                _screen(visit["screen"]),
                visit["description"],
                visit.get("drag_first", 0),
            )
            for visit in raw.get("visit", [])
        )
        start = raw.get("start", {})
        return App(
            raw["name"],
            raw["bundle"],
            raw.get("label", raw["bundle"]),
            _screen(raw["home"]),
            tuple(tabs.values()),
            visits,
            raw.get("install", "install it"),
            start.get("page"),
            start.get("url"),
            tuple((str(key), str(value)) for key, value in start.get("parameters", {}).items()),
            start.get("ability", "EntryAbility"),
        )
    except (KeyError, TypeError) as error:
        raise ValueError(f"{where}: app {raw.get('name', '?')!r} lacks or misuses {error}") from None


def load(paths: Sequence[Path]) -> list[App]:
    """The apps defined in `paths`, TOML files or folders of them. Each `[[app]]` has `name`, `bundle`, `label`,
    `install`, `home` (a screen: `url`, `tab`, `texts`, `tab_bar`), optionally `start` (`page` of the harness's page
    server or `url`, `parameters` and `ability`), `[[app.tab]]` tables (`name`, `label`, `url`, `scrolls`,
    `interstitial`) and `[[app.visit]]` tables (`name`, `tab` by name, `target` [x, y], `screen`, `description`,
    `drag_first`)."""
    files = [file for path in paths for file in (sorted(path.glob("*.toml")) if path.is_dir() else [path])]
    apps: dict[str, App] = {}
    for file in files:
        with open(file, "rb") as f:
            for raw in tomllib.load(f).get("app", []):
                app = _app(raw, str(file))
                if app.name in apps:
                    raise ValueError(f"{file}: a second app named {app.name!r}")
                apps[app.name] = app
    return list(apps.values())


def launch(spec: App, device: OhosDevice, artifacts: Path, start_url: str | None = None) -> Iterator[WebViewApp]:
    if not device.is_installed(spec.bundle):
        pytest.skip(f"{spec.bundle} is not installed: {spec.install}")
    app = WebViewApp(device, spec.bundle, artifacts, spec.ability)
    try:
        try:
            app.start(spec.home, start_url or spec.start_url, spec.parameters)
        except Exception:
            app.save_failure()
            raise
        yield app
    finally:
        try:
            app.finish()
        finally:
            app.stop()


def check_tab(app: WebViewApp, tab: Tab) -> None:
    opened = open_tab(app, tab)
    distance = app.drag(DRAG)
    up = app.capture("dragged-up", tab.screen, _dragged(opened, "down", distance, tab.scrolls))
    check_moved(app, opened, up, -1 if tab.scrolls else 0, distance)
    distance = app.drag(-DRAG_BACK)
    down = app.capture("dragged-down", tab.screen, _dragged(up, "back up", distance, tab.scrolls))
    check_moved(app, up, down, 1 if tab.scrolls else 0, distance)
    app.check_running()
    app.check_crashes()


def check_visit(app: WebViewApp, visit: Visit) -> None:
    opened = open_tab(app, visit.tab)
    if visit.drag_first:
        distance = app.drag(visit.drag_first)
        expect = _dragged(opened, "down", distance, True)
    else:
        expect = f"{opened.name} again"
    before = app.capture("before", visit.tab.screen, expect)
    app.tap(visit.target)
    where = f"{before.name}'s page {visit.target.x:.0%} across and {visit.target.y:.0%} down"
    shown = app.capture("opened", visit.screen, f"{visit.description}, opened by tapping {where}")
    distance = app.drag(DRAG)
    up = app.capture("dragged-up", visit.screen, _dragged(shown, "down", distance, True))
    check_moved(app, shown, up, -1, distance)
    app.back()
    back = app.capture("back", visit.tab.screen, f"{before.name} again, as it was: Back closed {shown.name}")
    difference = screenshots.difference(before.screenshot, back.screenshot, back.box)
    app.note(back, difference_to_before=round(difference, 1))
    assert difference < MAX_BACK_DIFFERENCE, f"the page looks different after going back (difference {difference:.1f})"
    app.check_running()
    app.check_crashes()


def open_tab(app: WebViewApp, tab: Tab) -> Capture:
    app.tap_tab(tab.label)
    return app.capture(tab.name, tab.screen, f"the {tab.label} tab's page", tab.interstitial)


def check_moved(app: WebViewApp, before: Capture, after: Capture, direction: int, distance: int) -> None:
    """Checks that the page moved up (direction -1), down (1) or not at all (0) from `before` to `after`, after a drag
    of `distance` pixels."""
    if direction == 0:
        difference = screenshots.difference(before.screenshot, after.screenshot, after.box)
        app.note(after, difference=round(difference, 1))
        assert difference < MAX_UNCHANGED, f"{after.name}: the page changed (difference {difference:.1f})"
        return
    shift = screenshots.vertical_shift(before.screenshot, after.screenshot, after.box, round(distance * MAX_SHIFT))
    if shift is None:
        # Live content, such as a feed that replaces its items, can change too much to tell; the reviewer can.
        app.note(after, shift=None, review="the content changed too much to tell from the pixels how far it moved")
        return
    app.note(after, shift=shift)
    expected = "up" if direction < 0 else "down"
    assert shift * direction >= MIN_SCROLL, f"{after.name}: the page did not scroll {expected} (moved {shift} px)"


def _dragged(before: Capture, way: str, distance: int, scrolls: bool) -> str:
    if not scrolls:
        return f"{before.name} unchanged: the page doesn't scroll"
    return f"{before.name} scrolled {way} by about {distance} px, or less where the page ends"
