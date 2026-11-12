"""The security pages of the SVG test pages, which servoshell must render in time, without crashing or hanging.

They probe the limits an SVG engine needs on nesting, reference cycles and expansion, which keep such content from
taking long: each page must load and render within `LIMIT` seconds, or the test ends as TIMEOUT. Layer 3 also expects
a Content-Security-Policy to keep an `<image>` from fetching from another origin, which
`test_csp_blocks_cross_origin_image` checks; it skips when servoshell doesn't fetch the `<image>` of inline SVG at all,
also without the policy. Layer 2 expects a `<style>` in inline SVG not to apply to the document and
not to fetch its `@import`s; that isn't checked, since in the specifications and other browsers it is a style sheet of
the document like any other.
"""

import pytest

from servo_embedder_tests.errors import ScenarioTimeout
from servo_embedder_tests.oracles import wait_until
from servo_embedder_tests.pages import PageServer
from servo_embedder_tests.svg_cards import PAGES

pytestmark = pytest.mark.explicit

SECURITY = PAGES / "security"
# Seconds a page may take to load and render; the slowest takes 1.5 s on a HiHope DAYU200.
LIMIT = 10
CSP = """<meta http-equiv="Content-Security-Policy" content="img-src 'self'">"""
IMAGE_URL = "https://attacker.example/collect"


@pytest.fixture
def servo(request, shared_servo):
    return shared_servo.reuse(request.node)


@pytest.fixture
def other_origin(platform):
    server = PageServer().start()
    platform.reverse(server.port)
    yield server
    server.stop()


@pytest.mark.scenario_timeout(LIMIT)
@pytest.mark.parametrize(
    "page", sorted(path.name for path in SECURITY.glob("*.html")), ids=lambda page: page.removesuffix(".html")
)
def test_renders(page, servo, pages, artifacts):
    driver = servo.driver
    driver.get(pages.url(f"svg/security/{page}"))
    driver.save_screenshot(str(artifacts / "screen.png"))


def test_csp_blocks_cross_origin_image(servo, pages, other_origin):
    source = (SECURITY / "layer3_fetch_allowlist.html").read_text(encoding="utf-8")
    assert CSP in source and IMAGE_URL in source
    url = f"http://127.0.0.1:{other_origin.port}/collect"
    with_csp = pages.add("svg-csp/with-csp.html", source.replace(IMAGE_URL, f"{url}-with-csp"))
    without_csp = pages.add("svg-csp/without-csp.html", source.replace(CSP, "").replace(IMAGE_URL, f"{url}-without"))
    driver = servo.driver
    driver.get(with_csp)
    # Once the page rendered, it would have requested the image.
    driver.get_screenshot_as_png()
    driver.get(without_csp)
    try:
        wait_until(lambda: "/collect-without" in other_origin.requests, message="the page without CSP to fetch")
    except ScenarioTimeout:
        pytest.skip("servoshell doesn't fetch the <image> without CSP either, so there is nothing for CSP to block")
    assert "/collect-with-csp" not in other_origin.requests, "the <image> was fetched despite img-src 'self'"
