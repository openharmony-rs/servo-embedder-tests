from servo_embedder_tests import plugin
from servo_embedder_tests.cli import _pytest_args, _Selection

pytest_plugins = ["pytester"]


def collected(pytester, *argv: str) -> list[str]:
    args, extra = _pytest_args(argv)
    recorder = pytester.inline_run("--collect-only", *args, plugins=[_Selection(extra)])
    return [plugin.test_key(call.item) for call in recorder.getcalls("pytest_itemcollected")]


def test_runs_bundled_suite_by_default(pytester):
    tests = collected(pytester, "--artifacts", "out/", "--expectations", "local.py")
    assert "navigation/test_local_pages.py::test_loads_local_page" in tests
    assert "ime/test_text_input.py::test_tap_shows_keyboard" in tests


def test_selects_bundled_folders_and_files(pytester):
    tests = collected(pytester, "navigation/", "sites/test_mossel.py::test_mossel_home")
    assert tests == [
        "navigation/test_local_pages.py::test_loads_local_page",
        "navigation/test_local_pages.py::test_history_back_and_forward",
        "sites/test_mossel.py::test_mossel_home",
    ]


def test_keeps_local_paths(pytester):
    pytester.mkdir("ime")
    (pytester.path / "ime" / "test_local.py").write_text("def test_local(): pass\n")
    assert collected(pytester, "ime/test_local.py", "--artifacts", "out/run") == ["ime/test_local.py::test_local"]


def test_keys_with_non_ascii_names_select_their_tests(pytester):
    pytester.makepyfile(
        test_names="""
        import pytest

        @pytest.mark.parametrize("name", ["具身智能", "人形机器人"])
        def test_page(name):
            pass
        """
    )
    assert collected(pytester, "test_names.py::test_page[具身智能]") == ["test_names.py::test_page[具身智能]"]
    escaped = "test_names.py::test_page[\\u4eba\\u5f62\\u673a\\u5668\\u4eba]"
    assert collected(pytester, escaped) == ["test_names.py::test_page[人形机器人]"]


def test_extra_adds_to_bundled_suite(pytester):
    pytester.mkdir("more")
    (pytester.path / "more" / "test_more.py").write_text("def test_more(): pass\n")
    tests = collected(pytester, "--extra", "more")
    assert "more/test_more.py::test_more" in tests
    assert "navigation/test_local_pages.py::test_loads_local_page" in tests
