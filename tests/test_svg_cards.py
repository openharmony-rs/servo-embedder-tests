import pytest

from servo_embedder_tests import cli, svg_cards
from servo_embedder_tests.page_scenarios import discover


def test_every_card_is_one_whole_svg():
    found = svg_cards.load()
    assert len({card.name for card in found}) == len(found) > 100
    for card in found:
        assert card.svg.startswith("<svg") and card.svg.endswith("</svg>")
        assert card.svg.count("<svg") == card.svg.count("</svg>")
        assert card.title and "<" not in card.page_title


def test_nested_svgs_stay_in_their_card():
    viewbox = [card for card in svg_cards.load() if card.page == "9_viewbox_scaling"]
    assert [card.name for card in viewbox] == [f"09_viewbox_scaling-0{number}" for number in (1, 2, 3, 4)]
    assert any(card.svg.count("<svg") > 1 for card in viewbox)


def test_card_page_shows_only_the_svg(tmp_path):
    (tmp_path / "1_page.html").write_text(
        "<html><head><title>T</title><style>svg { width: 10px }</style></head><body>"
        '<div class="test-card"><h3>A &amp; B</h3><svg><svg></svg><text>x</text></svg><p>no</p></div>'
        '<div class="test-card"><h3>C</h3><svg viewBox="0 0 1 1"/></div>'
        "</body></html>"
    )
    first, second = svg_cards.load(tmp_path)
    assert (first.name, first.title, first.svg) == ("01_page-01", "A & B", "<svg><svg></svg><text>x</text></svg>")
    assert (second.name, second.svg) == ("01_page-02", '<svg viewBox="0 0 1 1"/>')
    page = first.html()
    assert "svg { width: 10px }" in page and f"<body>\n{first.svg}\n</body>" in page and "<p>no</p>" not in page


def test_cards_are_written_as_page_scenarios(tmp_path, capsys):
    folder = tmp_path / "cards"
    cards = svg_cards.load()
    assert cli.main(["svg-cards", str(folder)]) == 0
    assert f"wrote {len(cards)} SVG cards" in capsys.readouterr().out
    scenarios = discover([folder])
    assert [scenario.name for scenario in scenarios] == [card.name for card in cards]
    first = scenarios[0]
    assert first.steps[0].options == {"segments": 1} and first.page_option("freeze") and first.page_option("errors")
    assert (first.directory / "index.html").read_text(encoding="utf-8") == cards[0].html()
    svg_cards.write(folder, cards)  # again, e.g. after an update


def test_cards_are_not_written_into_other_folders(tmp_path):
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(ValueError, match="has other files"):
        svg_cards.write(tmp_path, svg_cards.load())
    assert [path.name for path in tmp_path.iterdir()] == ["notes.txt"]
