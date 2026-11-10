"""Reference renderings of pages by ArkWeb, the system web engine, to compare Servo's renderings with.

A viewer app shows the pages in an ArkWeb `Web` component: `aa start -U <url>` must load the URL, also when the app is
already running. Pages say when they rendered with `report({ready: <name>, rect: [x, y, width, height], dpr,
userAgent})`, where `rect` is the part to compare, in CSS pixels. The references are PNG files in a folder per device,
with `references.json` noting how each was captured, so that later runs on the device can reuse them.
"""

from __future__ import annotations

import datetime
import json
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

from .errors import ScenarioError, ScenarioTimeout
from .oracles import wait_until
from .pages import PageServer
from .platform import OhosDevice
from .screenshots import Box
from .servoshell import ServoshellOhos

# ArkWeb's user agent has `ArkWeb/<version>`; a viewer whose `Web` component runs another engine doesn't.
ARKWEB = "ArkWeb/"


@dataclass(frozen=True)
class Rendered:
    """What a page reported once it rendered."""

    rect: tuple[float, float, float, float]
    dpr: float
    user_agent: str

    @property
    def size(self) -> tuple[int, int]:
        """The size of `rect` in CSS pixels."""
        return round(self.rect[2]), round(self.rect[3])

    def box(self, origin: tuple[int, int]) -> Box:
        """`rect` in screen pixels, for a viewport whose top left corner is at `origin`."""
        x, y, width, height = (value * self.dpr for value in self.rect)
        return round(origin[0] + x), round(origin[1] + y), round(origin[0] + x + width), round(origin[1] + y + height)


def wait_rendered(pages: PageServer, name: str, since: int, timeout: float = 30) -> Rendered:
    """Waits for the page `name` to report, at index `since` or later, that it rendered."""
    report = pages.wait_for_report(lambda report: report.get("ready") == name, timeout=timeout, since=since)
    if report is None:
        raise ScenarioTimeout(f"{name} did not report within {timeout}s that it rendered")
    x, y, width, height = report["rect"]
    return Rendered((x, y, width, height), float(report["dpr"]), str(report.get("userAgent", "")))


class ReferenceStore:
    """Reference renderings as `<directory>/<name>.png`."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._index = directory / "references.json"

    def get(self, name: str) -> Path | None:
        path = self.directory / f"{name}.png"
        return path if path.is_file() else None

    def info(self, name: str) -> dict[str, object]:
        """How the reference `name` was captured."""
        return self._read().get(name, {})

    def put(self, name: str, image: Path, info: dict[str, object]) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{name}.png"
        shutil.copyfile(image, path)
        index = self._read()
        index[name] = info
        self._index.write_text(json.dumps(dict(sorted(index.items())), ensure_ascii=False, indent=2) + "\n")
        return path

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            return json.loads(self._index.read_text())
        except FileNotFoundError:
            return {}


class ArkWebViewer:
    """The viewer app on an hdc device."""

    def __init__(
        self,
        device: OhosDevice,
        bundle: str,
        ability: str = "EntryAbility",
        parameters: Sequence[Sequence[str]] = (),
    ) -> None:
        """`parameters` are name-value pairs to start the app with, as `aa start --ps name value`."""
        self.device = device
        self.bundle = bundle
        # Starts any app that loads the URL it is started with, not only servoshell.
        self.app = ServoshellOhos(device, bundle, ability)
        self.parameters = [(name, value) for name, value in parameters]
        self._origin: tuple[int, int] | None = None

    def capture(self, url: str, name: str, pages: PageServer, dest: Path) -> dict[str, object]:
        """Shows `url`, keeps the part of the screen the page `name` reports at `dest` and returns how it was taken."""
        since = len(pages.reports)
        self.app.start(url, None, parameters=self.parameters)
        rendered = wait_rendered(pages, name, since)
        if ARKWEB not in rendered.user_agent:
            raise ScenarioError(f"{self.bundle} does not render with ArkWeb (user agent {rendered.user_agent!r})")
        self._settle(dest, rendered.box(self._viewport_origin()))
        return {
            "user_agent": rendered.user_agent,
            "dpr": rendered.dpr,
            "captured": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        }

    def stop(self) -> None:
        self.app.stop()
        self._origin = None

    def _viewport_origin(self) -> tuple[int, int]:
        if self._origin is None:
            webs = self.device.dump_layout().find_all(type="Web")
            if not webs:
                raise ScenarioError(f"{self.bundle} shows no Web component")
            left, top, _, _ = webs[-1].bounds
            self._origin = (left, top)
        return self._origin

    def _settle(self, dest: Path, box: Box, timeout: float = 15) -> None:
        """Takes screenshots until `box` shows the same in two in a row, and keeps it at `dest`."""
        with tempfile.TemporaryDirectory() as directory:
            screen = Path(directory) / "screen.png"
            last: Image.Image | None = None

            def same() -> bool:
                nonlocal last
                with Image.open(self.device.screenshot(screen)) as image:
                    current = image.convert("RGB").crop(box)
                stable = last is not None and ImageChops.difference(last, current).getbbox() is None
                last = current
                return stable

            wait_until(same, timeout=timeout, interval=0, message=f"{self.bundle} to show the same twice")
            assert last is not None
            last.save(dest)


class References:
    """The reference renderings of a run: those in `store`, and the ones it lacks, captured with `viewer` and added."""

    def __init__(self, store: ReferenceStore, viewer: ArkWebViewer | None, pages: PageServer) -> None:
        self.store = store
        self.viewer = viewer
        self.pages = pages
        self.errors: dict[str, str] = {}

    def prepare(self, urls: dict[str, str]) -> None:
        """Captures the references that the store lacks of the pages `urls` maps names to, all in one go, so that the
        viewer comes to the front once."""
        missing = {name: url for name, url in urls.items() if not self.store.get(name) and name not in self.errors}
        if not missing or self.viewer is None:
            return
        try:
            with tempfile.TemporaryDirectory() as directory:
                image = Path(directory) / "reference.png"
                for name, url in missing.items():
                    try:
                        info = self.viewer.capture(url, name, self.pages, image)
                    except Exception as error:
                        self.errors[name] = f"capturing ArkWeb's rendering failed: {error}"
                        continue
                    self.store.put(name, image, info)
        finally:
            self.viewer.stop()

    def get(self, name: str) -> Path | None:
        """The reference of `name`, or None if there is none and no viewer to capture one."""
        if name in self.errors:
            raise ScenarioError(self.errors[name])
        return self.store.get(name)
