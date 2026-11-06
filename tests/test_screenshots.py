import random

from PIL import Image, ImageDraw

from servo_embedder_tests import screenshots

BOX = (0, 100, 200, 300)


def _save(path, stripe_at=None):
    image = Image.new("RGB", (200, 400), "white")
    if stripe_at is not None:
        ImageDraw.Draw(image).rectangle((0, stripe_at, 200, stripe_at + 40), fill="black")
    image.save(path, quality=90)
    return path


def test_contrast_is_zero_for_a_flat_colour(tmp_path):
    assert screenshots.contrast(_save(tmp_path / "flat.jpeg"), BOX) < 1
    assert screenshots.contrast(_save(tmp_path / "stripe.jpeg", 150), BOX) > 50


def test_difference_only_counts_the_box(tmp_path):
    a = _save(tmp_path / "a.jpeg", 150)
    assert screenshots.difference(a, _save(tmp_path / "b.jpeg", 150), BOX) < 1
    assert screenshots.difference(a, _save(tmp_path / "c.jpeg", 220), BOX) > 30
    assert screenshots.difference(_save(tmp_path / "d.jpeg"), _save(tmp_path / "e.jpeg", 340), BOX) < 1


def _page(path, offset, header=0, seed=37):
    """A 200x800 page of random rows, scrolled by `offset` rows, under a fixed header of `header` rows."""
    rows = [random.Random(seed * 10_000 + y).choices(range(256), k=20) for y in range(1000)]
    image = Image.new("L", (200, 800))
    image.putdata(
        [(rows[y + offset] if y >= header else rows[-1 - y])[x // 10] for y in range(800) for x in range(200)]
    )
    image.save(path)
    return path


def test_vertical_shift_finds_the_scroll_under_a_sticky_header(tmp_path):
    box = (0, 0, 200, 800)
    top, scrolled = _page(tmp_path / "top.png", 0, header=60), _page(tmp_path / "scrolled.png", 120, header=60)
    assert screenshots.vertical_shift(top, scrolled, box, 400) == -120
    assert screenshots.vertical_shift(scrolled, top, box, 400) == 120
    assert screenshots.vertical_shift(top, top, box, 400) == 0


def test_vertical_shift_is_none_for_another_page(tmp_path):
    box = (0, 0, 200, 800)
    assert (
        screenshots.vertical_shift(_page(tmp_path / "a.png", 0), _page(tmp_path / "b.png", 0, seed=91), box, 400)
        is None
    )
