import pytest

from servo_embedder_tests.scenarios.webview_apps import flows
from servo_embedder_tests.scenarios.webview_apps.test_apps import BUNDLED
from servo_embedder_tests.ui import Layout
from servo_embedder_tests.webview_app import Point, Screen, content_area


def _node(type, bounds, text="", children=(), **attributes):
    return {"attributes": {"type": type, "bounds": bounds, "text": text, **attributes}, "children": list(children)}


def _tabs(selected):
    items = [
        _node("Column", f"[{i * 100},900][{i * 100 + 100},1000]", children=[_node("Text", "[0,0][10,10]", label)])
        for i, label in enumerate(("首页", "分类"))
    ]
    items[selected]["attributes"]["selected"] = "true"
    return _node("TabBar", "[0,900][200,1000]", children=items)


APP = Layout.from_json(
    _node(
        "root",
        "[0,0][200,1000]",
        children=[_node("Web", "[0,100][200,900]", "https://h/pages/sort/sort.html"), _tabs(1)],
    )
)
NATIVE = Layout.from_json(
    _node(
        "root",
        "[0,0][200,1000]",
        children=[_node("NavDestination", "[0,0][200,1000]"), _node("NavDestination", "[0,100][200,900]")]
        + [_node("Text", "[0,100][100,150]", "详情页")],
    )
)


def test_screen_matches_url_tab_and_texts():
    assert Screen(url="/pages/sort/sort.html", tab="分类").matches(APP)
    assert not Screen(url="/pages/sort/sort.html", tab="首页").matches(APP)
    assert not Screen(url="/pages/index/index.html").matches(APP)
    assert Screen(url="", tab_bar=True).matches(APP)
    assert not Screen(url="", tab_bar=False).matches(APP)
    assert Screen(texts=("详情页",), tab_bar=False).matches(NATIVE)
    assert not Screen(url="").matches(NATIVE)


def test_content_area_is_the_web_view_or_the_topmost_native_page():
    area = content_area(APP)
    assert area is not None and area.type == "Web"
    area = content_area(NATIVE)
    assert area is not None and area.bounds == (0, 100, 200, 900)


def test_app_definitions_load_from_toml(tmp_path):
    (bundled,) = flows.load([BUNDLED])
    assert bundled.name == "arkweb-test" and bundled.parameters == (("page", "tabs"),)
    assert bundled.ability == "EntryAbility"
    assert bundled.tabs[0].screen == Screen(url="/webview_apps/scroll.html", tab="Tab 1")
    shop = tmp_path / "shop.toml"
    shop.write_text(
        '[[app]]\nname = "shop"\nbundle = "com.example.shop"\nhome = { tab = "Home" }\n'
        'start = { ability = "MainAbility" }\n'
        '[[app.tab]]\nname = "home"\nlabel = "Home"\nurl = "/index.html"\n'
        '[[app.visit]]\nname = "product"\ntab = "home"\ntarget = [0.5, 0.3]\nscreen = { url = "/p.html" }\n'
        'description = "a product"\n'
    )
    (app,) = flows.load([shop])
    assert app.visits[0].tab is app.tabs[0] and app.visits[0].target == Point(0.5, 0.3)
    assert app.ability == "MainAbility"
    shop.write_text(shop.read_text().replace('tab = "home"', 'tab = "cart"'))
    with pytest.raises(ValueError, match="'shop' lacks or misuses 'cart'"):
        flows.load([shop])
