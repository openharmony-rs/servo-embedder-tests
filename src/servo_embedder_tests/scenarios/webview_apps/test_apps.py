"""Apps that show their pages in the system web view: the bundled definitions in apps/ and those --webview-apps names.
Each tab of an app is a test_tab, each visit a test_visit; see flows.py."""

from pathlib import Path

import pytest

from . import flows

pytestmark = [pytest.mark.platform("ohos"), pytest.mark.explicit]

BUNDLED = Path(__file__).parent / "apps"


def pytest_generate_tests(metafunc):
    if "case" not in metafunc.fixturenames:
        return
    apps = flows.load([BUNDLED, *metafunc.config.getoption("webview_apps")])
    items = "visits" if metafunc.function.__name__ == "test_visit" else "tabs"
    cases = [pytest.param((app, item), id=f"{app.name}-{item.name}") for app in apps for item in getattr(app, items)]
    none = pytest.param(None, id="none", marks=pytest.mark.skip(reason=f"no app defines {items}; see --webview-apps"))
    metafunc.parametrize("case", cases or [none])


@pytest.fixture
def app(case, device, artifacts, request):
    spec = case[0]
    url = request.getfixturevalue("pages").url(spec.start_page) if spec.start_page else None
    yield from flows.launch(spec, device, artifacts, url)


def test_tab(app, case):
    flows.check_tab(app, case[1])


def test_visit(app, case):
    flows.check_visit(app, case[1])
