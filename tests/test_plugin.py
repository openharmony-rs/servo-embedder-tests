"""Runs the plugin on desktop against a fake app, to check expectations handling end to end."""

import json
import textwrap

import pytest

from servo_embedder_tests import plugin
from servo_embedder_tests.platform import DesktopHost
from servo_embedder_tests.plugin import TEST_ARTIFACTS_MARKER

pytest_plugins = ["pytester"]


def run(pytester, expectations: str, *extra: str):
    pytester.makefile(".toml", expectations=expectations)
    pytester.makepyfile(
        test_fake=textwrap.dedent(
            """
            import pytest
            from servo_embedder_tests.errors import ScenarioTimeout

            def test_pass(): pass
            def test_fail(): assert False
            def test_timeout(): raise ScenarioTimeout("slow")
            @pytest.mark.platform("ohos")
            def test_ohos_only(): pass
            """
        )
    )
    result = pytester.runpytest(
        "--platform",
        "desktop",
        "--device-class",
        "linux",
        "--expectations",
        "expectations.toml",
        "--artifacts",
        "out",
        *extra,
    )
    return result, json.loads((pytester.path / "out" / "results.json").read_text())


def test_no_expectations_means_pass(pytester):
    result, data = run(pytester, "")
    result.assert_outcomes(passed=1, failed=2, skipped=1)
    assert data["results"]["test_fake.py::test_timeout"]["status"] == "TIMEOUT"
    assert (
        data["results"]["test_fake.py::test_timeout"]["message"] == "servo_embedder_tests.errors.ScenarioTimeout: slow"
    )
    assert data["results"]["test_fake.py::test_ohos_only"]["status"] == "SKIP"


def test_expected_failures_and_unexpected_pass(pytester):
    result, data = run(
        pytester,
        '["test_fake.py::test_fail"]\nlinux = "FAIL"\n'
        '["test_fake.py::test_timeout"]\ndefault = "TIMEOUT"\n'
        '["test_fake.py::test_pass"]\nlinux = "FAIL"\n',
    )
    result.assert_outcomes(failed=1, xfailed=2, skipped=1)
    result.stdout.fnmatch_lines(["*UNEXPECTED PASS (expected FAIL): test_fake.py::test_pass*"])
    assert data["device_class"] == "linux"


def test_expected_skip_does_not_run(pytester):
    result, data = run(pytester, '["test_fake.py::test_fail"]\ndefault = "SKIP"\n')
    result.assert_outcomes(passed=1, failed=1, skipped=2)


def test_keys_name_non_ascii_parameters_as_they_are(pytester):
    pytester.makefile(".toml", expectations='["test_names.py::test_page[具身智能]"]\nlinux = "FAIL"\n')
    pytester.makepyfile(
        test_names=textwrap.dedent(
            """
            import pytest

            @pytest.mark.parametrize("name", ["具身智能", "人形机器人"])
            def test_page(name, artifacts):
                assert name != "具身智能"
            """
        )
    )
    out = pytester.path / "out"
    result = pytester.runpytest(
        "--platform", "desktop", "--device-class", "linux", "--expectations", "expectations.toml", "--artifacts", "out"
    )
    result.assert_outcomes(passed=1, xfailed=1)
    keys = set(json.loads((out / "results.json").read_text(encoding="utf-8"))["results"])
    assert keys == {"test_names.py::test_page[具身智能]", "test_names.py::test_page[人形机器人]"}
    markers = list(out.glob(f"*/{TEST_ARTIFACTS_MARKER}"))
    assert {marker.read_text(encoding="utf-8").strip() for marker in markers} == keys
    assert len(markers) == 2 and all(marker.parent.name.isascii() for marker in markers)


def test_a_marker_shortens_the_timeout_of_a_test(pytester):
    pytester.makepyfile(
        test_slow=textwrap.dedent(
            """
            import time
            import pytest

            @pytest.mark.scenario_timeout(0.2)
            def test_marked(): time.sleep(2)

            def test_unmarked(): time.sleep(0.4)
            """
        )
    )
    pytester.runpytest("--platform", "desktop", "--device-class", "linux", "--artifacts", "out")
    results = json.loads((pytester.path / "out" / "results.json").read_text())["results"]
    assert results["test_slow.py::test_marked"]["status"] == "TIMEOUT"
    assert "exceeded its scenario_timeout of 0.2s" in results["test_slow.py::test_marked"]["message"]
    assert results["test_slow.py::test_unmarked"]["status"] == "PASS"


def test_reference_engine_version_needs_a_reference_engine(pytester):
    pytester.makepyfile(test_fake="def test_pass(): pass\n")
    result = pytester.runpytest("--platform", "desktop", "--reference-engine-version", "v1", "--artifacts", "out")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--reference-engine-version needs --reference-engine*"])


def test_rerun_clears_earlier_artifacts(pytester):
    out = pytester.path / "out"
    (out / "old_test").mkdir(parents=True)
    (out / "old_test" / "test-key.txt").write_text("old\n")
    (out / "servoshell.log").write_text("old\n")
    (out / "keep").mkdir()
    run(pytester, "")
    assert not (out / "old_test").exists() and not (out / "servoshell.log").exists()
    assert (out / "keep").is_dir()


def test_device_loss_keeps_results(pytester, monkeypatch):
    # The conftest below replaces it for the whole process; this restores it after the test.
    monkeypatch.setattr(DesktopHost, "remove_forwards", DesktopHost.remove_forwards)
    pytester.makeconftest(
        textwrap.dedent(
            """
            import pytest
            from servo_embedder_tests.plugin import ServoSession
            from servo_embedder_tests.platform import DesktopHost

            def gone(*args):
                raise OSError("device gone")

            DesktopHost.remove_forwards = gone

            class App:
                is_running = gone

                def stop(self):
                    pass

            @pytest.fixture
            def servo(request, scenario_run):
                yield ServoSession(scenario_run, App(), request.node, None, None)
            """
        )
    )
    pytester.makepyfile(
        test_fake=textwrap.dedent(
            """
            def test_pass(servo): pass
            def test_fail(servo): assert False, "broken"
            """
        )
    )
    pytester.runpytest("--platform", "desktop", "--device-class", "linux", "--artifacts", "out")
    data = json.loads((pytester.path / "out" / "results.json").read_text())
    assert data["results"]["test_fake.py::test_pass"]["status"] == "PASS"
    assert data["results"]["test_fake.py::test_fail"]["status"] == "FAIL"
    assert data["results"]["test_fake.py::test_fail"]["message"].startswith("AssertionError: broken")


def test_a_shared_servoshell_restarts_after_a_test_that_did_not_pass(pytester, monkeypatch):
    # The conftest below replaces it for the whole process; this restores it after the test.
    monkeypatch.setattr(plugin, "connect", plugin.connect)
    pytester.makeconftest(
        textwrap.dedent(
            """
            import pytest
            from servo_embedder_tests import plugin

            class Driver:
                page_source = ""

                def save_screenshot(self, path):
                    pass

                def quit(self):
                    pass

            plugin.connect = lambda port, alive: Driver()

            class App:
                starts = 0

                def start(self, url, port, prefs, args):
                    App.starts += 1

                def is_running(self):
                    return True

                def stop(self):
                    pass

            @pytest.fixture(scope="module")
            def shared(request, scenario_run):
                yield plugin.ServoSession(scenario_run, App(), request.node, None, None)

            @pytest.fixture
            def servo(request, shared):
                return shared.reuse(request.node)
            """
        )
    )
    pytester.makepyfile(
        test_fake=textwrap.dedent(
            """
            def test_first(servo): assert servo.app.starts == 1
            def test_still_the_first(servo): assert servo.app.starts == 1
            def test_fail(servo): assert False
            def test_restarted(servo): assert servo.app.starts == 2
            def test_not_again(servo): assert servo.app.starts == 2
            """
        )
    )
    result = pytester.runpytest("--platform", "desktop", "--device-class", "linux", "--artifacts", "out")
    result.assert_outcomes(passed=4, failed=1)


def test_plugins_add_and_replace_engines(pytester):
    pytester.makeconftest(
        textwrap.dedent(
            """
            from servo_embedder_tests.engines import ServoshellEngine

            class Fake(ServoshellEngine):
                def __init__(self, name):
                    self.name = name

            def pytest_servo_embedder_tests_engines(config, platform):
                return [Fake("webview-servo"), Fake("servoshell")]
            """
        )
    )
    pytester.makepyfile(
        test_fake=textwrap.dedent(
            """
            def test_engines(page_runs):
                assert sorted(page_runs.engines) == ["servoshell", "webview-servo"]
                assert type(page_runs.engines["servoshell"]).__name__ == "Fake"
                assert page_runs.engine == "webview-servo" and page_runs.reference == "servoshell"
            """
        )
    )
    result = pytester.runpytest(
        "--platform", "desktop", "--artifacts", "out", "--engine", "webview-servo", "--reference-engine", "servoshell"
    )
    result.assert_outcomes(passed=1)


def test_tests_without_test_ime_skip_before_servoshell_starts(pytester):
    pytester.makeconftest(
        textwrap.dedent(
            """
            import pytest

            @pytest.fixture
            def servo():
                raise AssertionError("servoshell started")

            @pytest.fixture
            def ime():
                pass
            """
        )
    )
    pytester.makepyfile(test_fake="def test_typing(servo, ime): pass\n")
    result = pytester.runpytest("--platform", "desktop", "--device-class", "linux", "--artifacts", "out", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*needs a test IME*"])
