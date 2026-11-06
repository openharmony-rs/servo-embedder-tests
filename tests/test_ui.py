from servo_embedder_tests.ui import Layout, Node

TREE = {
    "attributes": {"type": "root", "bounds": "[0,0][1000,2000]"},
    "children": [
        {"attributes": {"type": "TextInput", "text": "about:blank", "bounds": "[100,200][900,300]"}, "children": []},
        {"attributes": {"type": "Button", "text": "+", "id": "new-tab", "bounds": "[1000,200][1100,300]"}},
        {"attributes": {"type": "XComponent", "bounds": "[0,0][0,0]"}},
        {"attributes": {"type": "XComponent", "id": "servo-window-1", "bounds": "[0,400][1000,1900]"}},
    ],
}


def find(layout: Layout, **kwargs: str) -> Node:
    node = layout.find(**kwargs)
    assert node is not None
    return node


def test_find_by_type_text_and_id():
    layout = Layout.from_json(TREE)
    assert find(layout, type="TextInput").text == "about:blank"
    assert find(layout, text="+").center == (1050, 250)
    assert find(layout, id="new-tab").type == "Button"
    assert layout.find(text="missing") is None


def test_webview_is_largest_visible_xcomponent():
    webview = Layout.from_json(TREE).webview()
    assert webview is not None and webview.bounds == (0, 400, 1000, 1900)


def test_selected():
    layout = Layout.from_json(
        {
            "attributes": {"type": "TabBar", "bounds": "[0,0][200,100]"},
            "children": [
                {"attributes": {"type": "Column", "bounds": "[0,0][100,100]", "selected": "true"}},
                {"attributes": {"type": "Column", "bounds": "[100,0][200,100]", "selected": "false"}},
            ],
        }
    )
    assert [item.is_selected for item in layout.root.children] == [True, False]
