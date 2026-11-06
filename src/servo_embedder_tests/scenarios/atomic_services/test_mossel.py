"""Mossel's atomic service (莫塞尔商城), an ASCF app: native tabs, a web view per page."""

import pytest

from servo_embedder_tests.atomic_service import Point, Screen

from . import flows
from .flows import App, Visit, ascf_tab

pytestmark = [pytest.mark.platform("ohos"), pytest.mark.explicit]

CATEGORIES = ascf_tab("categories", "分类", "/pages/sort/sort.html")
MOSSEL = App(
    bundle="com.atomicservice.5765880207855116497",
    label="莫塞尔商城 (the round icon)",
    home=Screen(url="/pages/index/index.html", tab="首页"),
    tabs=(
        ascf_tab("home", "首页", "/pages/index/index.html"),
        CATEGORIES,
        ascf_tab("articles", "种草", "/pages/topic/articles.html"),
        ascf_tab("cart", "购物车", "/pages/tabbarcart.html"),
        ascf_tab("account", "我的", "/pages/user.html"),
    ),
    visits=(
        Visit(
            "product",
            CATEGORIES,
            Point(0.6, 0.23),
            Screen(url="/pages/product/product.html"),
            "the page of the first product in the category's list",
        ),
    ),
)


@pytest.fixture
def service(device, artifacts):
    yield from flows.launch(MOSSEL, device, artifacts)


@pytest.mark.parametrize("tab", MOSSEL.tabs, ids=lambda tab: tab.name)
def test_tab(service, tab):
    flows.check_tab(service, tab)


@pytest.mark.parametrize("visit", MOSSEL.visits, ids=lambda visit: visit.name)
def test_visit(service, visit):
    flows.check_visit(service, visit)
