"""Hooks that plugins can implement, e.g. in a conftest.py or a module passed with `-p`."""

from __future__ import annotations

import pytest


@pytest.hookspec
def pytest_servo_embedder_tests_engines(config: pytest.Config, platform: object) -> list[object]:  # type: ignore[empty-body]
    """Engines (`servo_embedder_tests.engines.Engine`) for page scenarios, selected by name with `--engine` and
    `--reference-engine`. One with the name of a built-in engine (`servoshell`, `arkweb`) replaces it. `platform` is
    the run's `OhosDevice` or `DesktopHost`. Engines must not start anything before their `open`."""
