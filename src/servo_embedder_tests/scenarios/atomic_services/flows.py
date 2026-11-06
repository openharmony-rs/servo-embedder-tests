"""What the atomic service scenarios do: open each tab and drag its page up and back down, and open a page from a
tab, drag it, and go back.

The checks are coarse: the page shows, isn't blank, moves the way it was dragged and comes back, and nothing crashes.
Each step in a test's steps.json says what its screenshot should show, for a reviewer to check the rest, and steps the
checks couldn't judge say why under "review".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from servo_embedder_tests import screenshots
from servo_embedder_tests.atomic_service import AtomicService, Capture, Point, Screen
from servo_embedder_tests.platform import OhosDevice

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


def ascf_tab(name: str, label: str, page: str, **kwargs) -> Tab:
    """A tab of an ASCF app, whose pages are web views; `page` is the end of the page's URL."""
    return Tab(name, label, Screen(url=page, tab=label), **kwargs)


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
    bundle: str
    # The name AppGallery lists it under.
    label: str
    home: Screen
    tabs: tuple[Tab, ...]
    visits: tuple[Visit, ...]


def launch(app: App, device: OhosDevice, artifacts: Path) -> Iterator[AtomicService]:
    if not device.is_installed(app.bundle):
        pytest.skip(f"{app.bundle} is not installed: open {app.label} from AppGallery once, which needs a HUAWEI ID")
    service = AtomicService(device, app.bundle, artifacts)
    try:
        try:
            service.start(app.home)
        except Exception:
            service.save_failure()
            raise
        yield service
    finally:
        try:
            service.finish()
        finally:
            service.stop()


def check_tab(service: AtomicService, tab: Tab) -> None:
    opened = open_tab(service, tab)
    distance = service.drag(DRAG)
    up = service.capture("dragged-up", tab.screen, _dragged(opened, "down", distance, tab.scrolls))
    check_moved(service, opened, up, -1 if tab.scrolls else 0, distance)
    distance = service.drag(-DRAG_BACK)
    down = service.capture("dragged-down", tab.screen, _dragged(up, "back up", distance, tab.scrolls))
    check_moved(service, up, down, 1 if tab.scrolls else 0, distance)
    service.check_running()
    service.check_crashes()


def check_visit(service: AtomicService, visit: Visit) -> None:
    opened = open_tab(service, visit.tab)
    if visit.drag_first:
        distance = service.drag(visit.drag_first)
        expect = _dragged(opened, "down", distance, True)
    else:
        expect = f"{opened.name} again"
    before = service.capture("before", visit.tab.screen, expect)
    service.tap(visit.target)
    where = f"{before.name}'s page {visit.target.x:.0%} across and {visit.target.y:.0%} down"
    shown = service.capture("opened", visit.screen, f"{visit.description}, opened by tapping {where}")
    distance = service.drag(DRAG)
    up = service.capture("dragged-up", visit.screen, _dragged(shown, "down", distance, True))
    check_moved(service, shown, up, -1, distance)
    service.back()
    back = service.capture("back", visit.tab.screen, f"{before.name} again, as it was: Back closed {shown.name}")
    difference = screenshots.difference(before.screenshot, back.screenshot, back.box)
    service.note(back, difference_to_before=round(difference, 1))
    assert difference < MAX_BACK_DIFFERENCE, f"the page looks different after going back (difference {difference:.1f})"
    service.check_running()
    service.check_crashes()


def open_tab(service: AtomicService, tab: Tab) -> Capture:
    service.tap_tab(tab.label)
    return service.capture(tab.name, tab.screen, f"the {tab.label} tab's page", tab.interstitial)


def check_moved(service: AtomicService, before: Capture, after: Capture, direction: int, distance: int) -> None:
    """Checks that the page moved up (direction -1), down (1) or not at all (0) from `before` to `after`, after a drag
    of `distance` pixels."""
    if direction == 0:
        difference = screenshots.difference(before.screenshot, after.screenshot, after.box)
        service.note(after, difference=round(difference, 1))
        assert difference < MAX_UNCHANGED, f"{after.name}: the page changed (difference {difference:.1f})"
        return
    shift = screenshots.vertical_shift(before.screenshot, after.screenshot, after.box, round(distance * MAX_SHIFT))
    if shift is None:
        # Live content, such as a feed that replaces its items, can change too much to tell; the reviewer can.
        service.note(after, shift=None, review="the content changed too much to tell from the pixels how far it moved")
        return
    service.note(after, shift=shift)
    expected = "up" if direction < 0 else "down"
    assert shift * direction >= MIN_SCROLL, f"{after.name}: the page did not scroll {expected} (moved {shift} px)"


def _dragged(before: Capture, way: str, distance: int, scrolls: bool) -> str:
    if not scrolls:
        return f"{before.name} unchanged: the page doesn't scroll"
    return f"{before.name} scrolled {way} by about {distance} px, or less where the page ends"
