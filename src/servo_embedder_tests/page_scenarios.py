"""Page scenarios: a folder per test page, rendered by an engine and compared with stored captures.

A scenarios folder has a subfolder per scenario with its `index.html` and resources. `page-scenarios.toml` in the
scenarios folder sets defaults for all, `scenario.toml` in a scenario's folder overrides them for it (see `DEFAULTS`).
A rendering is a series of captures, screenshots of the viewport once the page settled, each with a DOM snapshot.
Captures of an engine on a device are kept in a `CaptureStore`: the baselines a later rendering by the same engine must
match, and the renderings of a reference engine, such as ArkWeb, that another engine is compared with.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import json
import shutil
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait

from . import diffing
from .diffing import BASELINE, REFERENCE, CssBox, Preset, Shot
from .engines import Engine
from .errors import AppCrashed, ScenarioError, ScenarioTimeout
from .pages import PageServer

SUITE_CONFIG = "page-scenarios.toml"
SCENARIO_CONFIG = "scenario.toml"
# The configuration keys, with their defaults.
DEFAULTS: dict[str, object] = {
    # The page to open, relative to the scenario's folder.
    "url": "index.html",
    # Viewports the default steps capture from the top of the page; "all" scrolls to the end.
    "segments": 5,
    # CSS pixels between the captures of segments; 0: the viewport height. Set it to the smaller viewport height when
    # engines with different viewports are compared, so that their segments start at the same scroll positions.
    "segment_height": 0,
    # CSS selectors of elements that comparisons ignore, e.g. a clock.
    "mask": [],
    # Seconds a page may take to get ready, and each capture to settle.
    "timeout": 30,
    # Seconds without new resource loads after which a page counts as loaded.
    "quiet": 0.5,
    # Changes to the page, each opt-in and inserted at the start of its `<head>`: `freeze` is a style sheet that turns
    # off CSS animations and transitions (elements show their own style, not an animation's) and hides the caret and
    # scrollbars, and pauses media once the page is ready; `errors` is a script that records script errors, failed
    # resources and console.error calls, which then fail `test_renders`; `seed_random` is a script that makes the
    # page's `Math.random()` return the same numbers on every load and in every engine, as long as the page calls it in
    # the same order (not in workers, nor `crypto.getRandomValues()`).
    "page": {"freeze": False, "errors": False, "seed_random": False},
    # Overrides of the comparison presets (`diffing.Preset` fields).
    "compare": {"baseline": {}, "reference": {}},
    # The steps; without them, one capture named `page` of `segments` viewports.
    "steps": None,
}
# Captures of `segments = "all"` at most, in case a page grows as it scrolls.
MAX_SEGMENTS = 50
ACTIONS = {"capture": ("segments", "expect_change"), "hover": (), "click": (), "scroll": (), "wait_for": ()}
# Seconds between checks while a page loads.
POLL = 0.2

READY_STATE = """
return {
  state: document.readyState,
  images: Array.from(document.images).filter((image) => !image.complete).length,
  fonts: document.fonts ? document.fonts.status : null,
  resources: performance.getEntriesByType ? performance.getEntriesByType("resource").length : 0,
};
"""
NEXT_FRAMES = """
const done = arguments[arguments.length - 1];
requestAnimationFrame(() => requestAnimationFrame(() => done(true)));
"""
# Inserted before the page's own styles rather than once it loaded: Servo keeps the last animated value of a property
# when an animation is removed while it runs, instead of returning to the property's own value.
FREEZE = """<style id="servo-embedder-tests-freeze">
*, *::before, *::after {
  animation: none !important; transition: none !important; caret-color: transparent !important;
  scroll-behavior: auto !important;
}
html { scrollbar-width: none !important; }
::-webkit-scrollbar { display: none !important; }
</style>"""
PAUSE_MEDIA = 'for (const media of document.querySelectorAll("video, audio")) media.pause();'
ERRORS = """<script>(() => {
  const errors = window.__servoEmbedderTestsErrors = [];
  const add = (kind, message) => errors.length < 100 && errors.push({ kind, message: String(message).slice(0, 500) });
  addEventListener("error", (event) => {
    const target = event.target;
    if (target && target !== window && (target.src || target.href)) add("resource", target.src || target.href);
    else add("error", event.message + (event.filename ? ` (${event.filename}:${event.lineno})` : ""));
  }, true);
  addEventListener("unhandledrejection", (event) => {
    add("unhandledrejection", event.reason && event.reason.stack || event.reason);
  });
  const consoleError = console.error;
  console.error = function (...args) {
    add("console.error", args.map(String).join(" "));
    return consoleError.apply(this, args);
  };
})();</script>"""
# A seeded generator (mulberry32) in place of Math.random(): the engines seed theirs per load, and pages can't seed
# them. Changing the seed or the generator changes what pages that use it render, and so invalidates their baselines.
SEED_RANDOM = """<script>(() => {
  let state = 0x2f6b8e1d;
  const random = () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  random.servoEmbedderTests = true;
  Math.random = random;
})();</script>"""
# The `[page]` options that insert something at the start of the page's `<head>`, in the order they are inserted.
PAGE_CHANGES = (("freeze", FREEZE), ("errors", ERRORS), ("seed_random", SEED_RANDOM))
VIEWPORT = (
    "return [innerWidth, innerHeight, devicePixelRatio, scrollX, scrollY, document.scrollingElement.scrollHeight]"
)
EXTERNAL = """
return performance.getEntriesByType("resource").map((entry) => entry.name).filter((name) => {
  try {
    const url = new URL(name);
    return url.protocol.startsWith("http") && url.origin !== location.origin;
  } catch (error) {
    return false;
  }
});
"""
MASKS = """
const boxes = [];
for (const selector of arguments[0]) {
  for (const element of document.querySelectorAll(selector)) {
    const r = element.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) boxes.push([r.left, r.top, r.right, r.bottom]);
  }
}
return boxes;
"""
RECT = """
const element = document.querySelector(arguments[0]);
if (!element) return null;
const r = element.getBoundingClientRect();
return [r.left, r.top, r.right, r.bottom];
"""
# The load state of an image and its URL, the path for same-origin ones. "broken": loaded without a usable picture (a
# failed request, or data the engine can't decode). SVG without a natural size is never called broken.
IMAGE = """
const image = (element) => {
  const src = element.currentSrc || element.src || "";
  let shown = src.slice(0, 120);
  try {
    const url = new URL(src);
    if (url.origin === location.origin) shown = url.pathname + url.search;
  } catch (error) {}
  const svg = /\\.svg([?#]|$)|^data:image\\/svg/i.test(src);
  const state = !element.complete ? "loading"
    : element.naturalWidth > 0 || element.naturalHeight > 0 || svg ? "loaded" : "broken";
  return { image: state, src: shown };
};
"""
# Properties that change how an element is painted, when not at their initial value (diffing.PAINT_KEYS).
PAINT = """
const paint = (style) => {
  const found = {};
  // Same-origin URLs without the origin, whose port changes between runs.
  const cut = (value) => String(value).split(location.origin).join("").slice(0, 120);
  if (style.backgroundClip === "text" || style.webkitBackgroundClip === "text") found.background_clip = "text";
  if (style.backgroundImage && style.backgroundImage !== "none") found.background_image = cut(style.backgroundImage);
  if (style.clipPath && style.clipPath !== "none") found.clip_path = cut(style.clipPath);
  const mask = style.maskImage || style.webkitMaskImage;
  if (mask && mask !== "none") found.mask = cut(mask);
  if (style.filter && style.filter !== "none") found.filter = cut(style.filter);
  if (style.opacity && style.opacity !== "1") found.opacity = style.opacity;
  return found;
};
"""
# Bumped when DOM snapshots record more, so that comparisons with older stored ones skip what those lack.
DOM_VERSION = 2
# Elements with text of their own, replaced content or a non-initial paint property (PAINT) that overlap the
# viewport, with their box in viewport CSS pixels and the computed style a rendering difference often comes from.
DOM_SNAPSHOT = (
    IMAGE
    + PAINT
    + """
const path = (element) => {
  const parts = [];
  for (let e = element; e && e !== document.documentElement; e = e.parentElement) {
    if (e.id) { parts.unshift(`${e.localName}#${e.id}`); break; }
    let part = e.localName;
    const parent = e.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter((child) => child.localName === e.localName);
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(e) + 1})`;
    }
    parts.unshift(part);
  }
  return parts.join(">");
};
const round = (value) => Math.round(value * 10) / 10;
const elements = [];
for (const element of document.body ? document.body.querySelectorAll("*") : []) {
  const r = element.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) continue;
  if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
  let text = "";
  for (const node of element.childNodes) if (node.nodeType === 3) text += node.data;
  text = text.trim().replace(/\\s+/g, " ").slice(0, 80);
  const replaced = /^(img|svg|canvas|video|input|button|select|textarea|iframe)$/.test(element.localName);
  const style = getComputedStyle(element);
  const painted = paint(style);
  if (!text && !replaced && !Object.keys(painted).length) continue;
  const entry = {
    path: path(element), text, rect: [r.left, r.top, r.width, r.height].map(round),
    font_family: style.fontFamily, font_size: style.fontSize, font_weight: style.fontWeight,
    font_style: style.fontStyle, color: style.color, ...painted,
  };
  if (element.localName === "img") Object.assign(entry, image(element));
  elements.push(entry);
  if (elements.length >= 3000) break;
}
return elements;
"""
)
BROKEN_IMAGES = (
    IMAGE
    + """
return Array.from(document.images).map(image).filter((found) => found.image === "broken").map((found) => found.src);
"""
)


def _merge(base: dict[str, object], override: dict[str, object], where: str) -> dict[str, object]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key not in DEFAULTS:
            raise ValueError(f"{where}: unknown key {key!r}; known: {', '.join(DEFAULTS)}")
        default = DEFAULTS[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise ValueError(f"{where}: {key} must be a table")
            table = merged[key]
            assert isinstance(table, dict)
            for name, item in value.items():
                if name not in default:
                    raise ValueError(f"{where}: unknown key {key}.{name}; known: {', '.join(default)}")
                if isinstance(default[name], dict):
                    table[name] = {**table[name], **item}
                else:
                    table[name] = item
        else:
            merged[key] = value
    return merged


def _load(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass(frozen=True)
class Step:
    action: str
    target: object
    options: dict[str, object]

    @classmethod
    def parse(cls, raw: dict[str, object], where: str) -> Step:
        actions = [key for key in raw if key in ACTIONS]
        if len(actions) != 1:
            raise ValueError(f"{where}: a step has one of {', '.join(ACTIONS)}, got {raw}")
        action = actions[0]
        options = {key: value for key, value in raw.items() if key != action}
        unknown = set(options) - set(ACTIONS[action])
        if unknown:
            raise ValueError(f"{where}: {action} steps don't take {', '.join(sorted(unknown))}")
        return cls(action, raw[action], options)


@dataclass(frozen=True)
class Scenario:
    name: str
    directory: Path
    config: dict[str, object]

    @property
    def url_path(self) -> str:
        return f"{self.name}/{self.config['url']}"

    @property
    def steps(self) -> list[Step]:
        raw = self.config["steps"]
        if raw is None:
            raw = [{"capture": "page", "segments": self.config["segments"]}]
        assert isinstance(raw, list)
        return [Step.parse(step, f"{self.directory}/{SCENARIO_CONFIG}") for step in raw]

    @property
    def masks(self) -> list[str]:
        return list(self.config["mask"])  # type: ignore[call-overload]

    def preset(self, kind: str) -> Preset:
        overrides = self.config["compare"][kind]  # type: ignore[index]
        return (BASELINE if kind == "baseline" else REFERENCE).updated(overrides)

    def page_option(self, name: str) -> bool:
        return bool(self.config["page"][name])  # type: ignore[index]


def discover(roots: list[Path]) -> list[Scenario]:
    """The scenarios in the folders `roots`: their subfolders with the page to open."""
    scenarios: dict[str, Scenario] = {}
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"--page-scenarios {root} is not a folder")
        defaults = _merge(DEFAULTS, _load(root / SUITE_CONFIG), str(root / SUITE_CONFIG))
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            config = _merge(defaults, _load(directory / SCENARIO_CONFIG), str(directory / SCENARIO_CONFIG))
            if not (directory / str(config["url"])).is_file():
                continue
            if directory.name in scenarios:
                raise ValueError(
                    f"two scenarios named {directory.name}: {scenarios[directory.name].directory}, {directory}"
                )
            scenario = Scenario(directory.name, directory, config)
            scenario.steps  # noqa: B018 - validates them
            for kind in ("baseline", "reference"):
                try:
                    scenario.preset(kind)
                except TypeError as error:
                    raise ValueError(f"{directory}: compare.{kind}: {error}") from None
            scenarios[directory.name] = scenario
    return list(scenarios.values())


@dataclass
class Capture:
    """A screenshot of the viewport, `size` CSS pixels, at `scroll`, with its DOM snapshot (of `dom_version`) and
    masked boxes."""

    name: str
    image: Path
    dom: Path
    size: tuple[int, int]
    scroll: tuple[float, float]
    masks: list[CssBox]
    dom_version: int = DOM_VERSION

    @property
    def shot(self) -> Shot:
        return Shot(self.image, self.size, self.scroll[1], tuple(self.masks))

    def elements(self) -> list[dict[str, object]]:
        return json.loads(self.dom.read_text()) if self.dom.is_file() else []

    def meta(self) -> dict[str, object]:
        return {
            "size": list(self.size),
            "scroll": list(self.scroll),
            "masks": [list(box) for box in self.masks],
            "dom": self.dom_version,
        }

    @classmethod
    def from_meta(cls, name: str, directory: Path, meta: dict) -> Capture:
        return cls(
            name,
            directory / f"{name}.png",
            directory / f"{name}.dom.json",
            (int(meta["size"][0]), int(meta["size"][1])),
            (float(meta["scroll"][0]), float(meta["scroll"][1])),
            [(box[0], box[1], box[2], box[3]) for box in meta.get("masks", [])],
            int(meta.get("dom", 1)),
        )


@dataclass
class Rendering:
    """What rendering a scenario with an engine produced, in `directory`."""

    engine: str
    scenario: str
    directory: Path
    captures: dict[str, Capture] = field(default_factory=dict)
    info: dict[str, object] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    error: BaseException | None = None

    def write(self, device: str) -> None:
        data = {
            "engine": self.engine,
            "scenario": self.scenario,
            "device": device,
            "info": self.info,
            "problems": self.problems,
            "error": None if self.error is None else f"{type(self.error).__name__}: {self.error}",
            "captures": {name: capture.meta() for name, capture in self.captures.items()},
        }
        diffing.write_json(self.directory / "rendering.json", data)

    def raise_error(self) -> None:
        """Re-raises what made the rendering fail, so that a test that needs it fails the same way."""
        if self.error is not None:
            raise self.error


class CaptureStore:
    """Captures of one engine on one device: `<root>/<device>/<engine>/<scenario>/<capture>.png` and `.dom.json`, with
    `captures.json` noting their geometry and how they were made."""

    INDEX = "captures.json"

    def __init__(self, root: Path, device: str, engine: str) -> None:
        self.root = root
        self.device = device
        self.engine = engine

    def directory(self, scenario: str) -> Path:
        return self.root / self.device / self.engine / scenario

    def index(self, scenario: str) -> dict[str, object]:
        path = self.directory(scenario) / self.INDEX
        return json.loads(path.read_text()) if path.is_file() else {}

    def captures(self, scenario: str) -> dict[str, Capture]:
        directory = self.directory(scenario)
        raw = self.index(scenario).get("captures", {})
        assert isinstance(raw, dict)
        return {name: Capture.from_meta(name, directory, meta) for name, meta in raw.items()}

    def put(self, rendering: Rendering, reason: str | None = None) -> None:
        """Makes the captures of `rendering` the stored ones of its scenario."""
        directory = self.directory(rendering.scenario)
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        for name, capture in rendering.captures.items():
            shutil.copyfile(capture.image, directory / f"{name}.png")
            if capture.dom.is_file():
                shutil.copyfile(capture.dom, directory / f"{name}.dom.json")
        index = {
            "engine": rendering.info.get("engine", {}),
            "stored": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "reason": reason,
            "captures": {name: capture.meta() for name, capture in rendering.captures.items()},
        }
        diffing.write_json(directory / self.INDEX, index)


class Renderer:
    """Renders scenarios: opens the page with an engine, waits for it, runs the steps and captures."""

    def __init__(self, pages: PageServer) -> None:
        self.pages = pages
        self._pointer_used: set[str] = set()
        self._injected: set[str] = set()

    def render(self, engine: Engine, scenario: Scenario, directory: Path) -> Rendering:
        if directory.exists():
            for path in directory.iterdir():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        directory.mkdir(parents=True, exist_ok=True)
        rendering = Rendering(engine.name, scenario.name, directory)
        started = time.monotonic()
        try:
            self._render(engine, scenario, rendering)
        except Exception as error:
            rendering.error = error
            if not _running(engine):
                rendering.error = AppCrashed(f"{engine.name} exited while rendering {scenario.name}: {error}")
            else:
                try:
                    engine.screenshot(directory / "failure.png")
                except Exception:
                    pass
        rendering.info["seconds"] = round(time.monotonic() - started, 1)
        return rendering

    def _render(self, engine: Engine, scenario: Scenario, rendering: Rendering) -> None:
        timeout = float(scenario.config["timeout"])  # type: ignore[arg-type]
        if scenario.name not in self._injected:
            for option, snippet in PAGE_CHANGES:
                if scenario.page_option(option):
                    self.pages.inject(f"/{scenario.name}/", snippet)
            self._injected.add(scenario.name)
        driver = engine.open(self.pages.url(scenario.url_path))
        if engine.name in self._pointer_used:
            actions = ActionBuilder(driver)
            actions.pointer_action.move_to_location(0, 0)
            actions.perform()
            self._pointer_used.discard(engine.name)
        wait_ready(driver, timeout, float(scenario.config["quiet"]))  # type: ignore[arg-type]
        if scenario.page_option("freeze"):
            driver.execute_script(PAUSE_MEDIA)
            next_frames(driver)
        width, height, dpr, *_ = driver.execute_script(VIEWPORT)
        rendering.info.update(engine=engine.describe(), viewport=[width, height], dpr=dpr)
        for step in scenario.steps:
            self._step(engine, driver, scenario, step, rendering, timeout)
        rendering.info["external"] = sorted(set(driver.execute_script(EXTERNAL)))
        broken = sorted(set(driver.execute_script(BROKEN_IMAGES)))
        rendering.info["broken_images"] = broken
        if broken:
            rendering.problems.append(f"{len(broken)} image(s) did not load: {', '.join(broken[:5])}")
        if scenario.page_option("errors"):
            errors = driver.execute_script("return window.__servoEmbedderTestsErrors || null")
            rendering.info["errors"] = errors
            if errors is None:
                rendering.problems.append("the error recording script did not run")
            elif errors:
                rendering.problems.append(f"{len(errors)} error(s) on the page, the first: {errors[0]}")
        if scenario.page_option("seed_random") and not driver.execute_script(
            "return Math.random.servoEmbedderTests === true"
        ):
            rendering.problems.append(
                "Math.random() is not the seeded one: the script did not run, or the page replaced it"
            )

    def _step(
        self, engine: Engine, driver: WebDriver, scenario: Scenario, step: Step, rendering: Rendering, timeout: float
    ) -> None:
        target = step.target
        if step.action == "capture":
            segments = step.options.get("segments")
            if segments is None:
                self._capture(engine, driver, scenario, str(target), rendering, timeout, step)
            else:
                self._segments(engine, driver, scenario, str(target), segments, rendering, timeout)
            return
        if step.action == "wait_for":
            visible = "const e = document.querySelector(arguments[0]); return !!e && e.getClientRects().length > 0"
            try:
                WebDriverWait(driver, timeout).until(lambda d: d.execute_script(visible, target))
            except TimeoutException:
                raise ScenarioTimeout(f"{target} did not show within {timeout}s") from None
            return
        if step.action == "scroll":
            if isinstance(target, int | float):
                driver.execute_script("scrollTo(0, arguments[0])", target)
            elif target in ("top", "bottom"):
                driver.execute_script(
                    "scrollTo(0, arguments[0] ? document.scrollingElement.scrollHeight : 0)", target == "bottom"
                )
            else:
                driver.execute_script("arguments[0].scrollIntoView({block: 'start'})", _find(driver, str(target)))
            next_frames(driver)
            return
        element = _find(driver, str(target))
        if step.action == "hover":
            driver.execute_script("arguments[0].scrollIntoView({block: 'nearest'})", element)
            ActionChains(driver).move_to_element(element).perform()
        else:
            element.click()
        self._pointer_used.add(engine.name)
        next_frames(driver)

    def _segments(
        self,
        engine: Engine,
        driver: WebDriver,
        scenario: Scenario,
        name: str,
        segments: object,
        rendering: Rendering,
        timeout: float,
    ) -> None:
        count = MAX_SEGMENTS if segments == "all" else int(segments)  # type: ignore[call-overload]
        _, height, *_ = driver.execute_script(VIEWPORT)
        step = int(scenario.config["segment_height"]) or height  # type: ignore[call-overload]
        for index in range(count):
            driver.execute_script("scrollTo(0, arguments[0])", index * step)
            next_frames(driver)
            self._capture(engine, driver, scenario, f"{name}-{index}", rendering, timeout)
            _, height, _, _, scroll_y, total = driver.execute_script(VIEWPORT)
            if scroll_y + height >= total - 1:
                break
        driver.execute_script("scrollTo(0, 0)")

    def _capture(
        self,
        engine: Engine,
        driver: WebDriver,
        scenario: Scenario,
        name: str,
        rendering: Rendering,
        timeout: float,
        step: Step | None = None,
    ) -> Capture:
        if name in rendering.captures:
            raise ScenarioError(f"{scenario.name} captures {name} twice")
        previous = list(rendering.captures.values())[-1] if rendering.captures else None
        width, height, _, scroll_x, scroll_y, _ = driver.execute_script(VIEWPORT)
        directory = rendering.directory
        capture = Capture(
            name,
            directory / f"{name}.png",
            directory / f"{name}.dom.json",
            (round(width), round(height)),
            (scroll_x, scroll_y),
            [tuple(box) for box in driver.execute_script(MASKS, scenario.masks)],  # type: ignore[misc]
        )
        attempts = settle(engine, capture, timeout, lambda: driver.execute_script(DOM_SNAPSHOT))
        diffing.write_json(capture.dom, driver.execute_script(DOM_SNAPSHOT))
        rendering.captures[name] = capture
        rendering.info.setdefault("attempts", {})[name] = attempts  # type: ignore[union-attr]
        expected = step.options.get("expect_change") if step else None
        if expected and previous is None:
            rendering.problems.append(f"{name}: expect_change needs an earlier capture")
        elif expected and previous is not None:
            diff = diffing.compare(previous.shot, capture.shot, BASELINE.updated({"tolerance": 8}))
            area = driver.execute_script(RECT, expected) if isinstance(expected, str) else None
            if isinstance(expected, str) and area is None:
                rendering.problems.append(f"{name}: no element {expected} to expect a change in")
            elif diff.matches or (area and not any(_overlaps(region.box, area) for region in diff.regions)):
                where = f" in {expected}" if isinstance(expected, str) else ""
                rendering.problems.append(f"{name}: nothing changed{where} since {previous.name}")
        return capture


def _in_document(capture: Capture) -> list[dict[str, object]]:
    """The DOM snapshot of `capture` with boxes in document coordinates, so that snapshots taken at different scroll
    positions compare."""
    x, y = capture.scroll
    elements = capture.elements()
    for element in elements:
        left, top, width, height = element["rect"]  # type: ignore[misc]
        element["rect"] = [round(left + x, 1), round(top + y, 1), width, height]
    return elements


def _overlaps(a: CssBox, b: list[float]) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _running(engine: Engine) -> bool:
    try:
        return engine.is_running()
    except Exception:
        return True


def _find(driver: WebDriver, selector: str):  # noqa: ANN202
    elements = driver.find_elements(By.CSS_SELECTOR, selector)
    if not elements:
        raise ScenarioError(f"no element matches {selector}")
    return elements[0]


def next_frames(driver: WebDriver) -> None:
    driver.execute_async_script(NEXT_FRAMES)


def wait_ready(driver: WebDriver, timeout: float, quiet: float) -> None:
    """Waits until the page loaded, its images and fonts too, no resource loaded for `quiet` seconds and two
    animation frames passed."""
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    quiet_since = time.monotonic()
    while True:
        state = driver.execute_script(READY_STATE)
        if state.get("resources") != last.get("resources"):
            quiet_since = time.monotonic()
        last = state
        # Servo's WebDriver leaves out properties whose value is null.
        loaded = state.get("state") == "complete" and not state.get("images") and state.get("fonts") != "loading"
        if loaded and time.monotonic() - quiet_since >= quiet:
            break
        if time.monotonic() > deadline:
            raise ScenarioTimeout(f"the page did not finish loading within {timeout}s: {state}")
        time.sleep(POLL)
    next_frames(driver)


def _masked(path: Path, masks: list[CssBox], width: int) -> Image.Image:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
    scale = rgb.width / width
    draw = ImageDraw.Draw(rgb)
    for left, top, right, bottom in masks:
        draw.rectangle((left * scale, top * scale, right * scale - 1, bottom * scale - 1), fill=(255, 0, 255))
    return rgb


def settle(engine: Engine, capture: Capture, timeout: float, elements: Callable[[], list[dict[str, object]]]) -> int:
    """Takes screenshots until two in a row are the same outside the masks, keeps the last as the capture's image and
    returns how many it took. Raises ScenarioError naming what kept changing if that doesn't happen within `timeout`."""
    deadline = time.monotonic() + timeout
    previous: Image.Image | None = None
    candidate = capture.image.with_name(f"{capture.name}-unsettled-next.png")
    attempts = 0
    while True:
        attempts += 1
        engine.screenshot(candidate)
        current = _masked(candidate, capture.masks, capture.size[0])
        if previous is not None and ImageChops.difference(previous, current).getbbox() is None:
            candidate.replace(capture.image)
            return attempts
        if previous is not None and time.monotonic() > deadline:
            break
        candidate.replace(capture.image)
        previous = current
    before = Shot(capture.image, capture.size, 0, tuple(capture.masks))
    after = Shot(candidate, capture.size, 0, tuple(capture.masks))
    unsettled = capture.image.with_name(f"{capture.name}-unsettled.png")
    changing = Preset(tolerance=0, tile=16, tile_share=0, css=False)
    diff = diffing.compare(before, after, changing, overlay=unsettled)
    snapshot = elements()
    parts = []
    for region in diff.regions[:5]:
        left, top, right, bottom = (round(value) for value in region.box)
        inside = ", ".join(f"{e['path']} {e.get('text', '')!r}" for e in diffing.elements_in(snapshot, region.box, 2))
        parts.append(f"({left}, {top})-({right}, {bottom}) [{inside or 'no element with text'}]")
    raise ScenarioError(
        f"{capture.name} did not settle within {timeout}s ({attempts} screenshots); still changing: "
        + "; ".join(parts)
        + f". Mask it (mask = [...]) or freeze animations ([page] freeze = true); see {unsettled.name}"
    )


@dataclass
class Comparison:
    """The result of comparing a rendering with stored captures."""

    matches: bool
    failures: list[str]
    report: dict[str, object]

    def message(self, what: str) -> str:
        return f"{len(self.failures)} capture(s) differ from {what}: " + "; ".join(self.failures) + "; see diff.json"


def compare_captures(
    expected: dict[str, Capture],
    rendering: Rendering,
    preset: Preset,
    directory: Path,
    labels: tuple[str, str],
    reference: dict[str, Capture] | None = None,
    describe: Callable[[], dict[str, object]] | None = None,
) -> Comparison:
    """Compares the captures of `rendering` with `expected`, those with the same names, and writes `diff.json` and the
    review images of differing captures to `directory`. `reference` captures, from another engine, add a hint per
    differing region whether the change moved toward them."""
    directory.mkdir(parents=True, exist_ok=True)
    failures = []
    captures: dict[str, object] = {}
    for name in sorted(set(expected) - set(rendering.captures)):
        failures.append(f"{name}: not captured")
        captures[name] = {"matches": False, "summary": "not captured this time"}
    for name, actual in rendering.captures.items():
        stored = expected.get(name)
        if stored is None:
            failures.append(f"{name}: nothing stored to compare with")
            captures[name] = {
                "matches": False,
                "summary": "nothing stored to compare with",
                "actual": str(actual.image),
            }
            continue
        overlay = directory / f"{name}-differing.png"
        try:
            diff = diffing.compare(stored.shot, actual.shot, preset, overlay=overlay)
        except ValueError as error:
            failures.append(f"{name}: {error}")
            captures[name] = {"matches": False, "summary": str(error), "actual": str(actual.image)}
            continue
        entry: dict[str, object] = {"expected": str(stored.image), "actual": str(actual.image)}
        if not diff.matches:
            failures.append(f"{name}: {diff.summary()}")
            elements = actual.elements()
            broken = [element for element in elements if element.get("image") == "broken"]
            for region in diff.regions:
                region.elements = diffing.elements_in(elements, region.box)
            for region in diff.regions[: diffing.MAX_CROPS]:
                measurement = diffing.measure(stored.shot, actual.shot, diff, region)
                region.measured = asdict(measurement)
                region.reading = measurement.reading(text=any(element.get("text") for element in region.elements))
                missing = sorted({str(element["src"]) for element in diffing.elements_in(broken, region.box, 3)})
                if missing:
                    region.reading = f"image {', '.join(missing)} did not load in {labels[1]}; {region.reading}"
            if reference and name in reference:
                diffing.reference_hint(stored.shot, actual.shot, reference[name].shot, diff.regions)
            images = diffing.write_review_images(stored.shot, actual.shot, diff, overlay, directory, name, labels)
            entry["compare"] = images["compare"]
            entry["images"] = images
            entry["dom_changes"] = diffing.dom_changes(
                _in_document(stored), _in_document(actual), paint=min(stored.dom_version, actual.dom_version) >= 2
            )
        else:
            overlay.unlink(missing_ok=True)
        captures[name] = {**diff.to_dict(), **entry}
    report: dict[str, object] = {"labels": list(labels), "captures": captures}
    if describe is not None:
        report.update(describe())
    diffing.write_json(directory / "diff.json", report)
    return Comparison(not failures, failures, report)


class PageRuns:
    """The renderings of a run, one per engine and scenario, and the stored captures they are compared with."""

    def __init__(
        self,
        engines: dict[str, Engine],
        engine: str,
        reference: str | None,
        renderer: Renderer,
        baselines: Path | None,
        device: str,
        directory: Callable[[str], Path],
        update_baselines: bool = False,
        update_references: bool = False,
        versions: Mapping[str, str] | None = None,
    ) -> None:
        """`versions` names the builds of engines, e.g. a commit, for whoever compares their renderings."""
        if engine not in engines:
            raise ScenarioError(f"no engine {engine!r}; known: {', '.join(sorted(engines))}")
        self.engines = engines
        self.engine = engine
        self.reference = reference
        self.renderer = renderer
        self.baselines = baselines
        self.device = device
        self.directory = directory
        self.update_baselines = update_baselines
        self.update_references = update_references
        self.versions = dict(versions or {})
        self._renderings: dict[tuple[str, str], Rendering] = {}

    def rendering(self, engine: str, scenario: Scenario) -> Rendering:
        key = (engine, scenario.name)
        if key not in self._renderings:
            rendering = self.renderer.render(
                self.engines[engine], scenario, self.directory(f"{engine}/{scenario.name}")
            )
            if version := self.versions.get(engine):
                described = rendering.info.get("engine")
                rendering.info["engine"] = {
                    **(described if isinstance(described, dict) else {"engine": engine}),
                    "version": version,
                }
            rendering.write(self.device)
            self._renderings[key] = rendering
        return self._renderings[key]

    def store(self, engine: str) -> CaptureStore | None:
        return CaptureStore(self.baselines, self.device, engine) if self.baselines else None

    def stored_reference(self, scenario: Scenario) -> dict[str, Capture]:
        store = self.store(self.reference) if self.reference else None
        return store.captures(scenario.name) if store else {}

    def reference_captures(self, scenario: Scenario) -> tuple[dict[str, Capture], dict[str, object]] | None:
        """The reference engine's captures of `scenario`, stored or rendered now (and then stored), with how they
        were made; None if there are none and no engine to render them."""
        assert self.reference is not None
        store = self.store(self.reference)
        stored = store.captures(scenario.name) if store else {}
        if stored and not (self.update_references and self.reference in self.engines):
            assert store is not None
            return stored, {**store.index(scenario.name), "from": str(store.directory(scenario.name))}
        if self.reference not in self.engines:
            return None
        rendering = self.rendering(self.reference, scenario)
        rendering.raise_error()
        if store is not None:
            store.put(rendering, reason="reference rendering")
        return rendering.captures, {"engine": rendering.info.get("engine"), "from": "rendered in this run"}

    def stop(self) -> None:
        for engine in self.engines.values():
            with contextlib.suppress(Exception):
                engine.stop()
