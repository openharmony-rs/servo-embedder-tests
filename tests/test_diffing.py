import random
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFont

from servo_embedder_tests import diffing
from servo_embedder_tests.diffing import BASELINE, Shot

SIZE = (480, 1400)


def _page(path, scale=1.5, bold_heading=False, noise=0, shift=0):
    """A long text page of SIZE CSS pixels at `scale` device pixels per CSS pixel."""
    image = Image.new("RGB", (round(SIZE[0] * scale), round(SIZE[1] * scale)), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=round(14 * scale))
    heading = ImageFont.load_default(size=round(22 * scale))
    draw.text(
        (16 * scale, (20 + shift) * scale),
        "Order summary",
        font=heading,
        fill="black",
        stroke_width=1 if bold_heading else 0,
        stroke_fill="black",
    )
    for line in range(60):
        draw.text(
            (16 * scale, (60 + line * 22) * scale),
            f"Item {line}: noodles, tea and dumplings",
            font=font,
            fill=(40, 40, 40),
        )
    if noise:
        rng = random.Random(1)
        pixels = image.load()
        for _ in range(image.width * image.height // 50):
            x, y = rng.randrange(image.width), rng.randrange(image.height)
            r, g, b = pixels[x, y]
            pixels[x, y] = tuple(max(0, min(255, c + rng.randint(-noise, noise))) for c in (r, g, b))
    image.save(path)
    return Shot(path, SIZE)


def test_identical_captures_match(tmp_path):
    diff = diffing.compare(_page(tmp_path / "a.png"), _page(tmp_path / "b.png"), BASELINE)
    assert diff.matches
    assert diff.summary() == "matches"


def test_a_bolder_heading_fails_although_little_of_the_page_differs(tmp_path):
    diff = diffing.compare(_page(tmp_path / "a.png"), _page(tmp_path / "b.png", bold_heading=True), BASELINE)
    assert not diff.matches
    assert diff.differing / (diff.size[0] * diff.size[1] * diff.scale**2) < 0.01
    assert len(diff.regions) == 1
    left, top, right, bottom = diff.regions[0].box
    assert left < 30 and top < 30 and bottom < 60 and right < 260


def test_noise_below_the_tolerance_matches(tmp_path):
    diff = diffing.compare(_page(tmp_path / "a.png"), _page(tmp_path / "b.png", noise=20), BASELINE)
    assert diff.matches


def test_masks_of_either_capture_hide_a_region(tmp_path):
    a, b = _page(tmp_path / "a.png"), _page(tmp_path / "b.png", bold_heading=True)
    assert diffing.compare(a, replace(b, masks=((0, 0, 480, 56),)), BASELINE).matches
    assert diffing.compare(replace(a, masks=((0, 0, 480, 56),)), b, BASELINE).matches


def _shapes(path, scale):
    image = Image.new("RGB", (100 * scale, 50 * scale), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10 * scale, 10 * scale, 40 * scale, 40 * scale), fill="blue")
    draw.ellipse((60 * scale, 10 * scale, 90 * scale, 40 * scale), fill="red")
    image.save(path)
    return Shot(path, (100, 50))


def test_different_resolutions_are_compared_in_css_pixels(tmp_path):
    diff = diffing.compare(_shapes(tmp_path / "a.png", 1), _shapes(tmp_path / "b.png", 3), BASELINE)
    assert "resolutions differ" in diff.notes[0]
    assert diff.scale == 1


def test_different_viewports_compare_the_common_area(tmp_path):
    a = _page(tmp_path / "a.png")
    with Image.open(a.path) as image:
        image.crop((0, 0, image.width, 1000 * 3 // 2)).save(tmp_path / "short.png")
    diff = diffing.compare(a, Shot(tmp_path / "short.png", (480, 1000)), BASELINE)
    assert diff.matches
    assert diff.size == (480, 1000)
    assert "compared the 480x1000 from 0" in diff.notes[0]


def test_captures_scrolled_differently_are_compared_where_they_show_the_same_part(tmp_path):
    a = _page(tmp_path / "a.png", bold_heading=True)
    with Image.open(a.path) as image:
        image.crop((0, 30 * 3 // 2, image.width, 830 * 3 // 2)).save(tmp_path / "low.png")
        image.crop((0, 0, image.width, 800 * 3 // 2)).save(tmp_path / "high.png")
    low, high = Shot(tmp_path / "low.png", (480, 800), 30), Shot(tmp_path / "high.png", (480, 800), 0)
    diff = diffing.compare(high, low, BASELINE)
    assert diff.matches and diff.size == (480, 770) and diff.offset == 0
    plain = Shot(_page(tmp_path / "plain.png").path, SIZE)
    diff = diffing.compare(plain, low, BASELINE)
    assert len(diff.regions) == 1
    assert diff.regions[0].box[1] < 10


def test_review_images_and_region_crops(tmp_path):
    a, b = _page(tmp_path / "a.png"), _page(tmp_path / "b.png", bold_heading=True)
    diff = diffing.compare(a, b, BASELINE, overlay=tmp_path / "overlay.png")
    compare = diffing.write_review_images(a, b, diff, tmp_path / "overlay.png", tmp_path, "page-0", ("old", "new"))
    assert compare.is_file()
    crop = tmp_path / diff.regions[0].crop
    with Image.open(crop) as image:
        assert 16 < image.height and image.width <= 3 * 512 + 12


def test_reference_hint_tells_whether_a_change_moved_toward_the_reference(tmp_path):
    old, new = _page(tmp_path / "old.png"), _page(tmp_path / "new.png", bold_heading=True)
    diff = diffing.compare(old, new, BASELINE)
    diffing.reference_hint(old, new, _page(tmp_path / "ref.png", bold_heading=True), diff.regions)
    assert diff.regions[0].reference.startswith("closer")
    diffing.reference_hint(old, new, _page(tmp_path / "ref2.png"), diff.regions)
    assert diff.regions[0].reference.startswith("further")


def _element(path, rect, **style):
    return {
        "path": path,
        "text": "Order summary",
        "rect": rect,
        "font_family": "sans-serif",
        "font_size": "22px",
        "font_weight": "400",
        **style,
    }


def test_dom_changes_report_style_first_then_geometry():
    before = [_element("body>h1", [16, 20, 200, 30]), _element("body>p", [16, 60, 300, 20])]
    after = [_element("body>h1", [16, 20, 212, 30], font_weight="700"), _element("body>p", [16, 64, 300, 20])]
    changes = diffing.dom_changes(before, after)
    assert changes[0]["path"] == "body>h1" and changes[0]["style"] == {"font_weight": ["400", "700"]}
    assert changes[1]["path"] == "body>p" and "style" not in changes[1]


def test_elements_in_a_region_are_the_smallest_first():
    elements = [_element("body>div", [0, 0, 480, 400]), _element("body>div>h1", [16, 20, 200, 30])]
    found = diffing.elements_in(elements, (10, 10, 100, 40))
    assert [e["path"] for e in found] == ["body>div>h1", "body>div"]
