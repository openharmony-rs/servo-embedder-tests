import random
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
    images = diffing.write_review_images(a, b, diff, tmp_path / "overlay.png", tmp_path, "page-0", ("old", "new"))
    assert all((tmp_path / name).is_file() for name in images.values())
    with Image.open(tmp_path / images["actual"]) as actual, Image.open(b.path) as capture:
        assert actual.size == capture.size
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


def _measured(a, b):
    diff = diffing.compare(a, b, BASELINE)
    return diffing.measure(a, b, diff, diff.regions[0])


def test_measurements_find_a_shift(tmp_path):
    measurement = _measured(_page(tmp_path / "a.png"), _page(tmp_path / "b.png", shift=1))
    assert measurement.offset[0] == 0 and abs(measurement.offset[1] - 1) <= 0.5
    assert measurement.reading().startswith("moved by (+0, +")


def test_measurements_see_bolder_strokes(tmp_path):
    measurement = _measured(_page(tmp_path / "a.png"), _page(tmp_path / "b.png", bold_heading=True))
    assert measurement.ink > 1.08
    assert not measurement.reading().startswith("moved")
    assert "more ink: thicker or darker strokes" in measurement.reading()


def test_anti_aliasing_is_told_from_a_missing_texture(tmp_path):
    a = _page(tmp_path / "a.png")
    with Image.open(a.path) as image:
        expected = image.convert("RGB")
    edges = expected.convert("L").filter(ImageFilter.FIND_EDGES).point(lambda value: 255 if value > 24 else 0)
    shifted = Image.eval(expected, lambda value: max(0, value - 14))
    edged = Image.composite(shifted, expected, edges)
    edged.save(tmp_path / "edged.png")
    anti_aliased = _measured(a, Shot(tmp_path / "edged.png", SIZE))
    assert anti_aliased.on_edges >= 0.9 and anti_aliased.reading().startswith("only edge pixels differ")
    textured = expected.copy()
    pixels = textured.load()
    for y in range(0, 300, 3):
        for x in range(300, textured.width, 3):
            pixels[x, y] = (232, 232, 232)
    textured.save(tmp_path / "textured.png")
    missing = _measured(Shot(tmp_path / "textured.png", SIZE), a)
    assert missing.on_edges < 0.5 and missing.reading().startswith("faint differences away from shared edges")


def _line(path, spacing=0.0, stretch=1.0, scale=1.5):
    """One line of text, 480x60 CSS px, with extra letter spacing or glyphs stretched horizontally."""
    font = ImageFont.load_default(size=round(15 * scale))
    text = "06:06 to 10:23, 4h17m Order summary"
    glyphs = Image.new("L", (round(1200 * scale), round(60 * scale)), 255)
    draw = ImageDraw.Draw(glyphs)
    x = 0.0
    for char in text:
        draw.text((x, 20 * scale), char, font=font, fill=40)
        x += draw.textlength(char, font=font) + spacing * scale
    glyphs = glyphs.resize((round(glyphs.width * stretch), glyphs.height), Image.Resampling.LANCZOS)
    image = Image.new("RGB", (round(480 * scale), round(60 * scale)), "white")
    image.paste(
        glyphs.crop((0, 0, image.width - round(16 * scale), image.height)).convert("RGB"), (round(16 * scale), 0)
    )
    image.save(path)
    return Shot(path, (480, 60))


def test_letter_spacing_is_told_from_wider_glyphs(tmp_path):
    base = _line(tmp_path / "base.png")
    spaced = _measured(base, _line(tmp_path / "spaced.png", spacing=0.3))
    assert "letter spacing" in spaced.reading() and spaced.pieces_aligned >= 0.6
    stretched = _measured(base, _line(tmp_path / "stretched.png", stretch=1.04))
    assert "letter spacing" not in stretched.reading() and "shapes differ" in stretched.reading()


def test_review_crops_are_stacked(tmp_path):
    a, b = _page(tmp_path / "a.png"), _page(tmp_path / "b.png", bold_heading=True)
    diff = diffing.compare(a, b, BASELINE, overlay=tmp_path / "overlay.png")
    diffing.write_review_images(a, b, diff, tmp_path / "overlay.png", tmp_path, "page-0", ("old", "new"))
    with Image.open(tmp_path / diff.regions[0].crop) as crop:
        assert crop.height > crop.width / 3 and crop.width <= diffing.CROP_WIDTH


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
