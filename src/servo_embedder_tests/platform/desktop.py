"""Desktop hosts: the app runs on the machine that runs the tests."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator


class DesktopHost:
    family = "desktop"

    def __repr__(self) -> str:
        return "DesktopHost()"

    def device_class(self) -> str:
        if sys.platform.startswith("linux"):
            return "linux"
        if sys.platform == "darwin":
            return "macos"
        if sys.platform == "win32":
            return "windows"
        return sys.platform

    def name(self) -> str:
        return self.device_class()

    def describe(self) -> dict[str, str | None]:
        return {"os": sys.platform}

    def forward(self, port: int) -> None:
        pass

    def reverse(self, port: int) -> None:
        pass

    def remove_forwards(self) -> None:
        pass

    @contextlib.contextmanager
    def awake(self) -> Iterator[None]:
        yield
