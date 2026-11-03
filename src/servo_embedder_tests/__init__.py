"""Embedder tests for Servo's servoshell."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("servo-embedder-tests")
except PackageNotFoundError:
    __version__ = "0.0.0"

from .cli import run_tests  # noqa: E402

__all__ = ["__version__", "run_tests"]
