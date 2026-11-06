from servo_embedder_tests.atomic_service import Screen, content_area
from servo_embedder_tests.ui import Layout


def _node(type, bounds, text="", children=(), **attributes):
    return {"attributes": {"type": type, "bounds": bounds, "text": text, **attributes}, "children": list(children)}


def _tabs(selected):
    items = [
        _node("Column", f"[{i * 100},900][{i * 100 + 100},1000]", children=[_node("Text", "[0,0][10,10]", label)])
        for i, label in enumerate(("首页", "分类"))
    ]
    items[selected]["attributes"]["selected"] = "true"
    return _node("TabBar", "[0,900][200,1000]", children=items)


ASCF = Layout.from_json(
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
    assert Screen(url="/pages/sort/sort.html", tab="分类").matches(ASCF)
    assert not Screen(url="/pages/sort/sort.html", tab="首页").matches(ASCF)
    assert not Screen(url="/pages/index/index.html").matches(ASCF)
    assert Screen(url="", tab_bar=True).matches(ASCF)
    assert not Screen(url="", tab_bar=False).matches(ASCF)
    assert Screen(texts=("详情页",), tab_bar=False).matches(NATIVE)
    assert not Screen(url="").matches(NATIVE)


def test_content_area_is_the_web_view_or_the_topmost_native_page():
    area = content_area(ASCF)
    assert area is not None and area.type == "Web"
    area = content_area(NATIVE)
    assert area is not None and area.bounds == (0, 100, 200, 900)
