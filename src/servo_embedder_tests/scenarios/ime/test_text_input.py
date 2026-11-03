"""Text input through the platform input method framework, driven by the test IME from ohos-ime.

The page reports its events to the fixture server, so these tests work without WebDriver. Each test asserts the
behaviour an editor should have; servoshell gaps show up as expected failures in the embedder's expectations.
"""

import pytest
from ohos_test_ime import LEFT

from servo_embedder_tests.errors import ScenarioTimeout
from servo_embedder_tests.touch import tap_element, webview_origin

pytestmark = pytest.mark.platform("ohos")


def _wait(pages, predicate, message, since=0, timeout=10):
    report = pages.wait_for_report(predicate, timeout=timeout, since=since)
    if report is None:
        raise ScenarioTimeout(f"timed out waiting for {message}; reports: {pages.reports[since:][-8:]}")
    return report


def _value(pages, element, value, since=0):
    _wait(
        pages,
        lambda r: r["type"] == "input" and r["target"] == element and r["value"] == value,
        f"{element} to hold {value!r}",
        since,
    )


def open_and_focus(servo, device, pages, ime, element="field", webdriver=False):
    """Loads the IME page, taps `element` through the OS and waits for the IME to attach."""
    pages.clear_reports()
    servo.start(pages.url("ime.html"), webdriver=webdriver)
    geometry = _wait(pages, lambda r: r["type"] == "geometry", "the page to load", timeout=30)
    left, top = webview_origin(device)
    center = geometry[element]
    device.tap(round(left + center["x"] * geometry["dpr"]), round(top + center["y"] * geometry["dpr"]))
    _wait(pages, lambda r: r["type"] == "focus" and r["target"] == element, f"{element} to get focus")
    state = ime.wait_attached()
    assert state["attached"], f"the keyboard was not requested: {state}"
    return len(pages.reports)


def test_tap_shows_keyboard(servo, device, pages, ime):
    open_and_focus(servo, device, pages, ime)
    assert ime.state()["shown"]


@pytest.mark.parametrize(
    "text",
    ["hello", "wörld ß", "😀👍🏽", "é"],
    ids=["ascii", "latin", "emoji", "combining"],
)
def test_insert_text(servo, device, pages, ime, text):
    since = open_and_focus(servo, device, pages, ime)
    ime.insert(text)
    _value(pages, "field", text, since)


def test_commit_is_plain_text_insertion(servo, device, pages, ime):
    """Committed text without a preceding preview is `insertText`, not a composition, as in other browsers."""
    since = open_and_focus(servo, device, pages, ime)
    ime.insert("x")
    _value(pages, "field", "x", since)
    events = [r for r in pages.reports[since:] if r["target"] == "field"]
    assert not [r for r in events if r["type"].startswith("composition")], events
    beforeinput = [r for r in events if r["type"] == "beforeinput"]
    assert [(r["inputType"], r["data"], r["isComposing"]) for r in beforeinput] == [("insertText", "x", False)]


@pytest.mark.parametrize("cluster", ["👍🏽", "é"], ids=["emoji-modifier", "combining-mark"])
def test_backspace_deletes_grapheme(servo, device, pages, ime, cluster):
    since = open_and_focus(servo, device, pages, ime)
    ime.insert("ab" + cluster)
    _value(pages, "field", "ab" + cluster, since)
    ime.backspace()
    _value(pages, "field", "ab", since)


def test_composition_with_preview_text(servo, device, pages, ime):
    since = open_and_focus(servo, device, pages, ime)
    ime.preview("ni")
    _wait(pages, lambda r: r["type"] == "compositionupdate" and r["data"] == "ni", "compositionupdate 'ni'", since)
    ime.preview("你")
    _wait(pages, lambda r: r["type"] == "compositionupdate" and r["data"] == "你", "compositionupdate '你'", since)
    ime.finish_preview()
    _wait(pages, lambda r: r["type"] == "compositionend" and r["data"] == "你", "compositionend '你'", since)
    _value(pages, "field", "你", since)


def test_text_around_cursor(servo, device, pages, ime):
    since = open_and_focus(servo, device, pages, ime)
    ime.insert("hello")
    _value(pages, "field", "hello", since)
    assert ime.before(10) == "hello"
    assert ime.after(10) == ""
    assert ime.cursor() == 5


def test_move_cursor(servo, device, pages, ime):
    since = open_and_focus(servo, device, pages, ime)
    ime.insert("ac")
    _value(pages, "field", "ac", since)
    ime.move(LEFT)
    ime.insert("b")
    _value(pages, "field", "abc", since)


def test_enter_in_textarea_inserts_line_break(servo, device, pages, ime):
    since = open_and_focus(servo, device, pages, ime, element="area")
    ime.insert("a")
    _value(pages, "area", "a", since)
    ime.enter()
    _value(pages, "area", "a\n", since)
    assert ime.state()["attached"], "the keyboard went away after Enter in a textarea"


def test_keyboard_in_webdriver_mode(servo, device, pages, ime):
    """servoshell started for WebDriver must still show the keyboard for a real tap."""
    pages.clear_reports()
    servo.start(pages.url("ime.html"))
    from selenium.webdriver.common.by import By

    tap_element(device, servo.driver, servo.driver.find_element(By.ID, "field"))
    state = ime.wait_attached()
    assert state["attached"], "servoshell in WebDriver mode did not request the keyboard"
