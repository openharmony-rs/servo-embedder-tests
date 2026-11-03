"""Runs the plugin on desktop against a fake app, to check expectations handling end to end."""

import json
import textwrap

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


def test_rerun_clears_earlier_artifacts(pytester):
    out = pytester.path / "out"
    (out / "old_test").mkdir(parents=True)
    (out / "old_test" / "test-key.txt").write_text("old\n")
    (out / "servoshell.log").write_text("old\n")
    (out / "keep").mkdir()
    run(pytester, "")
    assert not (out / "old_test").exists() and not (out / "servoshell.log").exists()
    assert (out / "keep").is_dir()


def test_device_loss_keeps_results(pytester):
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
