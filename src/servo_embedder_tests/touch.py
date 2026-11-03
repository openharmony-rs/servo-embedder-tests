"""Real touch input on web content, at coordinates computed from WebDriver.

WebDriver clicks enter Servo below the platform: they never pass through ArkUI or servoshell's touch handling.
Use these helpers when that path is what's being tested, e.g. focus moving between ArkUI and the page.
"""

from __future__ import annotations

from dataclasses import dataclass

from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from .errors import ScenarioError
from .platform import OhosDevice

_GEOMETRY = """
const [element, scroll] = arguments;
if (scroll) element.scrollIntoView({block: "center", inline: "center"});
const r = element.getBoundingClientRect();
const left = Math.max(r.left, 0), top = Math.max(r.top, 0);
const right = Math.min(r.right, innerWidth), bottom = Math.min(r.bottom, innerHeight);
const x = (left + right) / 2, y = (top + bottom) / 2;
const hit = right > left && bottom > top ? document.elementFromPoint(x, y) : null;
return {x, y, dpr: devicePixelRatio, inView: right > left && bottom > top,
        hits: hit !== null && (hit === element || element.contains(hit))};
"""


@dataclass(frozen=True)
class ScreenPoint:
    x: int
    y: int
    client_x: float
    client_y: float


def webview_origin(device: OhosDevice) -> tuple[int, int]:
    node = device.dump_layout().webview()
    if node is None:
        raise ScenarioError("No XComponent in the layout; is servoshell in the foreground?")
    left, top, _, _ = node.bounds
    return left, top


def element_point(driver: WebDriver, element: WebElement, origin: tuple[int, int], scroll: bool = True) -> ScreenPoint:
    """Screen pixel at the centre of the visible part of `element`, checked to hit it like WebDriver's click."""
    geometry = driver.execute_script(_GEOMETRY, element, scroll)
    if not geometry["inView"]:
        raise ScenarioError("Element is not in the viewport")
    if not geometry["hits"]:
        raise ScenarioError("Element is obscured at its centre point")
    dpr = geometry["dpr"]
    return ScreenPoint(
        round(origin[0] + geometry["x"] * dpr),
        round(origin[1] + geometry["y"] * dpr),
        geometry["x"],
        geometry["y"],
    )


def tap_element(
    device: OhosDevice, driver: WebDriver, element: WebElement, origin: tuple[int, int] | None = None
) -> ScreenPoint:
    """Taps `element` through the OS input stack. Assumes no pinch zoom: Servo has no `visualViewport` to read it."""
    point = element_point(driver, element, origin or webview_origin(device))
    device.tap(point.x, point.y)
    return point
