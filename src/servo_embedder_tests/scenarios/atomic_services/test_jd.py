"""JD's atomic service (京东购物), an ASCF app: native tabs, a web view per page. Without a JD account, the
messages and cart tabs may open a login page in front."""

import pytest

from servo_embedder_tests.atomic_service import Point, Screen

from . import flows
from .flows import App, Visit, ascf_tab

pytestmark = [pytest.mark.platform("ohos"), pytest.mark.explicit]

LOGIN = Screen(url="/pages/login/index/index.html")
HOME = ascf_tab("home", "首页", "/pages/index/index.html")
JD = App(
    bundle="com.atomicservice.6917594172732814448",
    label="京东购物",
    home=HOME.screen,
    tabs=(
        HOME,
        ascf_tab(
            "messages", "消息", "/pages/msgCenter/pages/messageCenter/index.html", scrolls=False, interstitial=LOGIN
        ),
        ascf_tab("cart", "购物车", "/pages/cart/cart/index.html", scrolls=False, interstitial=LOGIN),
        ascf_tab("account", "我的", "/pages/my/index/index.html"),
    ),
    # The home page's campaigns change, so it scrolls the product feed into view first and accepts any page.
    visits=(
        Visit(
            "product",
            HOME,
            Point(0.25, 0.5),
            Screen(url="", tab_bar=False),
            "a product's page, or a campaign's if the feed shows one there",
            drag_first=0.5,
        ),
    ),
)


@pytest.fixture
def service(device, artifacts):
    yield from flows.launch(JD, device, artifacts)


@pytest.mark.parametrize("tab", JD.tabs, ids=lambda tab: tab.name)
def test_tab(service, tab):
    flows.check_tab(service, tab)


@pytest.mark.parametrize("visit", JD.visits, ids=lambda visit: visit.name)
def test_visit(service, visit):
    flows.check_visit(service, visit)
