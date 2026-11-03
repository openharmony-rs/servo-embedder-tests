"""Waiting for observable state instead of sleeping."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from .errors import ScenarioTimeout

T = TypeVar("T")


def wait_until(condition: Callable[[], T], timeout: float = 15, interval: float = 0.2, message: str = "condition") -> T:
    """Polls `condition` until it returns a truthy value, which is returned. Raises ScenarioTimeout."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise ScenarioTimeout(f"timed out after {timeout}s waiting for {message}")
        time.sleep(interval)
