"""`servo-embedder-tests run | list | doctor | update-expectations | accept | report | record-dump`."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from . import __version__
from .expectations import Expectations, RunReport, update
from .page_scenarios import Capture, CaptureStore, Rendering
from .plugin import BUNDLED_SCENARIOS
from .report import write_report


def _bundled(arg: str) -> str | None:
    """`arg` as a path into the bundled scenarios, if it names a folder (`ime/`) or file (`ime/test_x.py::name`) there
    and no such path exists locally."""
    if arg.startswith("-") or not ("/" in arg or ".py" in arg):
        return None
    path = arg.split("::", 1)[0]
    if Path(path).exists() or not (BUNDLED_SCENARIOS / path).exists():
        return None
    return str(BUNDLED_SCENARIOS / arg)


def _escaped(arg: str) -> str:
    """`arg` with the non-ASCII characters of its test name escaped, as pytest writes them in test ids, so that a test
    key (see `test_key`) selects its test."""
    path, separator, name = arg.partition("::")
    return path + separator + name.encode("ascii", "backslashreplace").decode("ascii")


class _Selection:
    """Runs the bundled suite unless the arguments name tests, finds named tests in it, and adds the `--extra`
    directories. Works on the positional arguments as pytest parsed them, so option values are never tests."""

    def __init__(self, extra: list[str]) -> None:
        self.extra = extra

    def pytest_configure(self, config: pytest.Config) -> None:
        if config.args_source == pytest.Config.ArgsSource.ARGS:
            args = [_escaped(_bundled(arg) or arg) for arg in config.args]
        elif config.getoption("page_scenarios", None):
            args = [str(BUNDLED_SCENARIOS / "page_scenarios")]
        else:
            args = [str(BUNDLED_SCENARIOS)]
        config.args = args + self.extra


def _pytest_args(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """The pytest arguments for `argv`, and the directories passed with `--extra DIR`."""
    args: list[str] = []
    extra: list[str] = []
    iterator = iter(argv)
    for arg in iterator:
        if arg == "--extra":
            extra.append(next(iterator))
        elif arg.startswith("--extra="):
            extra.append(arg.split("=", 1)[1])
        else:
            args.append(arg)
    platform = [] if any(a == "--platform" or a.startswith("--platform=") for a in args) else ["--platform", "ohos"]
    quiet = ["-W", "ignore::pytest.PytestAssertRewriteWarning"]
    # Without it, pytest derives the rootdir from existing paths among plugin option values.
    rootdir = [] if any(a.startswith("--rootdir") for a in args) else [f"--rootdir={Path.cwd()}"]
    return ["-p", "no:cacheprovider", *quiet, *rootdir, *platform, *args], extra


def run_tests(argv: Sequence[str] | None = None) -> int:
    """Runs the scenarios with pytest. `argv` takes pytest and servo-embedder-tests options (see `--help`)."""
    args, extra = _pytest_args(sys.argv[1:] if argv is None else argv)
    return int(pytest.main(args, plugins=[_Selection(extra)]))


def _doctor(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="servo-embedder-tests doctor")
    parser.add_argument("--platform", choices=("ohos", "desktop"), default="ohos")
    parser.add_argument("--hdc-target")
    parser.add_argument("--bundle", default="org.servo.servo")
    parser.add_argument("--servo-bin", type=Path, default=Path("target/release/servoshell"))
    options = parser.parse_args(argv)
    problems = 0

    def check(ok: bool, what: str, hint: str = "") -> None:
        nonlocal problems
        print(f"{'ok  ' if ok else 'FAIL'} {what}" + (f" -- {hint}" if hint and not ok else ""))
        problems += not ok

    print(f"servo-embedder-tests {__version__}")
    if options.platform == "ohos":
        hdc = shutil.which("hdc")
        check(hdc is not None, "hdc on PATH", "install the OpenHarmony SDK toolchains")
        if hdc is None:
            return 1
        targets = [
            t
            for t in subprocess.run([hdc, "list", "targets"], capture_output=True, text=True).stdout.split()
            if t != "[Empty]"
        ]
        check(bool(targets), f"hdc targets: {', '.join(targets) or 'none'}", "connect a device")
        if len(targets) > 1 and not options.hdc_target:
            check(False, "one target selected", "pass --hdc-target")
            return 1
        if targets:
            from .platform import OhosDevice

            device = OhosDevice(options.hdc_target)
            print(f"     {device.target}: {device.describe()} class={device.device_class()}")
            check(device.is_installed(options.bundle), f"{options.bundle} installed", "pass --hap to `run`")
            check(not device.is_screen_locked(), "screen unlocked (or no lock screen)", "the harness swipes it away")
    else:
        check(options.servo_bin.is_file(), f"servoshell at {options.servo_bin}", "pass --servo-bin")
    from .proxy import mitmdump_command

    try:
        check(True, f"mitmdump: {' '.join(mitmdump_command())}")
    except Exception as error:
        check(False, "mitmdump (only needed for --proxy)", str(error))
    return 1 if problems else 0


def _update_expectations(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="servo-embedder-tests update-expectations",
        description="Rewrite an expectations file so that the given runs have no unexpected results.",
    )
    parser.add_argument("results", type=Path, nargs="+", help="results.json files written by `run`")
    parser.add_argument("--expectations", type=Path, required=True)
    parser.add_argument("--intermittent", action="store_true", help="add new statuses instead of replacing")
    parser.add_argument("--prune", action="store_true", help="drop entries for tests the runs didn't collect")
    options = parser.parse_args(argv)
    expectations = Expectations.load(options.expectations)
    changes = update(
        expectations, [RunReport.from_file(p) for p in options.results], options.intermittent, options.prune
    )
    expectations.save(options.expectations)
    print("\n".join(changes) if changes else "expectations unchanged")
    return 0


BASELINE_TEST = "page_scenarios/test_pages.py::test_matches_baseline["


def _accept(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="servo-embedder-tests accept",
        description="Store the captures of page scenarios from a run as their baselines. Without scenario names, "
        "those whose baseline test failed.",
    )
    parser.add_argument("artifacts", type=Path, help="the run's --artifacts folder")
    parser.add_argument("scenarios", nargs="*", help="scenario names (default: the failed ones)")
    parser.add_argument("--baselines", type=Path, required=True)
    parser.add_argument("--engine", default="servoshell", help="the engine whose captures to store")
    parser.add_argument("--reason", help="why the new rendering is right, kept with the baseline")
    options = parser.parse_args(argv)
    renderings = {}
    for path in sorted(options.artifacts.glob("*/rendering.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["engine"] == options.engine:
            renderings[data["scenario"]] = (path.parent, data)
    selected = list(options.scenarios)
    if not selected:
        report = RunReport.from_file(options.artifacts / "results.json")
        selected = [
            key[len(BASELINE_TEST) : -1]
            for key, result in report.results.items()
            if key.startswith(BASELINE_TEST) and result.status == "FAIL"
        ]
    missing = [name for name in selected if name not in renderings]
    if missing:
        print(f"error: no {options.engine} rendering of {', '.join(missing)} in {options.artifacts}")
        return 1
    for name in selected:
        directory, data = renderings[name]
        if data["error"]:
            print(f"error: the rendering of {name} failed ({data['error']}); not storing it")
            return 1
        rendering = Rendering(
            data["engine"],
            name,
            directory,
            {capture: Capture.from_meta(capture, directory, meta) for capture, meta in data["captures"].items()},
            data["info"],
        )
        store = CaptureStore(options.baselines, data["device"], options.engine)
        store.put(rendering, options.reason)
        print(f"stored {len(rendering.captures)} capture(s) of {name} in {store.directory(name)}")
    if not selected:
        print("nothing to accept")
    return 0


def _report(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="servo-embedder-tests report",
        description="Write the page scenario report of a run again, e.g. after an agent added its conclusions.",
    )
    parser.add_argument("artifacts", type=Path, help="the run's --artifacts folder")
    options = parser.parse_args(argv)
    path = write_report(options.artifacts)
    if path is None:
        print(f"error: no page scenarios ran in {options.artifacts}")
        return 1
    print(f"wrote {path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "run": "run the scenarios (takes pytest options; `run -- --help` lists them)",
        "list": "list the scenarios that `run` would run",
        "doctor": "check hdc, the device and the app",
        "update-expectations": "rewrite expectations from results.json files",
        "accept": "store page scenario captures of a run as baselines: accept ARTIFACTS --baselines DIR",
        "report": "write a run's page scenario report (report.html) again: report ARTIFACTS",
        "record-dump": "record a mitmproxy dump of the network scenarios: record-dump PATH [run options]",
    }
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in commands:
        print("usage: servo-embedder-tests <command> [options]\n")
        for name, help in commands.items():
            print(f"  {name:20} {help}")
        print(f"\nservo-embedder-tests {__version__}")
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    command, rest = argv[0], argv[1:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if command == "run":
        return run_tests(rest)
    if command == "list":
        return run_tests(["--collect-only", "-q", *rest])
    if command == "doctor":
        return _doctor(rest)
    if command == "update-expectations":
        return _update_expectations(rest)
    if command == "accept":
        return _accept(rest)
    if command == "report":
        return _report(rest)
    if not rest:
        print("usage: servo-embedder-tests record-dump PATH [run options]")
        return 2
    dump = Path(rest[0])
    code = run_tests(["-m", "network", "--proxy", "record", "--proxy-dump", str(dump), *rest[1:]])
    if not dump.is_file() or dump.stat().st_size == 0:
        print(f"error: {dump} is empty; servoshell did not use the proxy")
        return code or 1
    print(f"recorded {dump.stat().st_size} bytes to {dump}")
    return code


if __name__ == "__main__":
    sys.exit(main())
