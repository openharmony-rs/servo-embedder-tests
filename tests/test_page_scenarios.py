import json
import shutil
import subprocess
import urllib.request
from types import SimpleNamespace
from typing import cast

import pytest
from PIL import Image, ImageDraw

from servo_embedder_tests import cli
from servo_embedder_tests.engines import ArkWebEngine
from servo_embedder_tests.page_scenarios import (
    ERRORS,
    PAGE_CHANGES,
    SEED_RANDOM,
    Capture,
    CaptureStore,
    Rendering,
    compare_captures,
    discover,
)
from servo_embedder_tests.pages import PageServer
from servo_embedder_tests.platform import OhosDevice
from servo_embedder_tests.plugin import TEST_ARTIFACTS_MARKER


def _scenarios(tmp_path, suite="", **scenarios):
    root = tmp_path / "genui"
    root.mkdir()
    if suite:
        (root / "page-scenarios.toml").write_text(suite)
    for name, config in scenarios.items():
        (root / name).mkdir()
        (root / name / "index.html").write_text("<!DOCTYPE html><html><head><title>t</title></head><body></body>")
        if config:
            (root / name / "scenario.toml").write_text(config)
    return root


def test_discover_merges_suite_defaults_and_scenario_overrides(tmp_path):
    root = _scenarios(
        tmp_path,
        "segments = 2\nmask = ['.clock']\n[page]\nfreeze = true\n",
        menu="segments = 'all'\n[page]\nseed_random = true\n[compare.baseline]\ntolerance = 4\n",
        order="",
    )
    (root / "notes").mkdir()
    menu, order = discover([root])
    assert [menu.name, order.name] == ["menu", "order"]
    assert menu.steps[0].options == {"segments": "all"} and order.steps[0].options == {"segments": 2}
    assert menu.masks == [".clock"] and menu.page_option("freeze") and not menu.page_option("errors")
    assert menu.page_option("seed_random") and not order.page_option("seed_random")
    assert menu.preset("baseline").tolerance == 4 and order.preset("baseline").tolerance == 12
    assert menu.url_path == "menu/index.html"


def test_steps_replace_the_default_capture(tmp_path):
    steps = (
        "[[steps]]\ncapture = 'top'\n[[steps]]\nhover = '#buy'\n[[steps]]\ncapture = 'hover'\nexpect_change = '#buy'\n"
    )
    (scenario,) = discover([_scenarios(tmp_path, cart=steps)])
    assert [(s.action, s.target) for s in scenario.steps] == [
        ("capture", "top"),
        ("hover", "#buy"),
        ("capture", "hover"),
    ]
    assert scenario.steps[2].options == {"expect_change": "#buy"}


@pytest.mark.parametrize(
    "config, error",
    [
        ("segmnets = 3\n", "unknown key 'segmnets'"),
        ("[page]\nfrozen = true\n", "unknown key page.frozen"),
        ("[[steps]]\nhover = '#a'\nclick = '#b'\n", "one of"),
        ("[[steps]]\nhover = '#a'\nsegments = 2\n", "hover steps don't take segments"),
        ("[compare.reference]\ntolerence = 3\n", "compare.reference"),
    ],
)
def test_configuration_mistakes_are_errors(tmp_path, config, error):
    with pytest.raises(ValueError, match=error):
        discover([_scenarios(tmp_path, page=config)])


def test_the_error_script_is_injected_only_under_its_prefix(tmp_path):
    root = _scenarios(tmp_path, menu="", order="")
    server = PageServer([root]).start()
    try:
        server.inject("/menu/", ERRORS)
        menu = urllib.request.urlopen(server.url("menu/index.html")).read().decode()
        order = urllib.request.urlopen(server.url("order/index.html")).read().decode()
    finally:
        server.stop()
    assert menu.startswith("<!DOCTYPE html><html><head><script>(() => {")
    assert "__servoEmbedderTestsErrors" not in order


def test_page_changes_come_before_the_pages_own_scripts(tmp_path):
    root = tmp_path / "pages"
    (root / "stars").mkdir(parents=True)
    page = "<!DOCTYPE html><html><head><script>placeStars()</script></head><body></body></html>"
    (root / "stars" / "index.html").write_text(page)
    server = PageServer([root]).start()
    try:
        for _, snippet in PAGE_CHANGES:
            server.inject("/stars/", snippet)
        served = urllib.request.urlopen(server.url("stars/index.html")).read().decode()
    finally:
        server.stop()
    positions = [served.index(snippet) for _, snippet in PAGE_CHANGES]
    assert positions == sorted(positions) and positions[-1] < served.index("placeStars()")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_seeded_math_random_returns_its_fixed_numbers():
    # These numbers are in the baselines of every page that uses seed_random: changing them invalidates those.
    script = SEED_RANDOM.removeprefix("<script>").removesuffix("</script>")
    check = "console.log(JSON.stringify([Math.random.servoEmbedderTests, ...Array.from({ length: 3 }, Math.random)]))"
    marker, *numbers = json.loads(subprocess.check_output(["node", "-e", script + check], text=True))
    assert marker is True
    assert numbers == [0.27861995808780193, 0.8225720841437578, 0.2360540598165244]


def _rendering(directory, engine="servoshell", heading="Menu", device="rk3568", photo=None):
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (480, 300), "white")
    ImageDraw.Draw(image).text((20, 20), heading, fill="black", font_size=28)
    if photo == "loaded":
        ImageDraw.Draw(image).rectangle((20, 100, 220, 220), fill=(40, 120, 200))
    image.save(directory / "page-0.png")
    dom = [{"path": "body>h1", "text": heading, "rect": [20, 20, 120, 34], "font_weight": "400"}]
    if photo:
        dom.append({"path": "body>img", "text": "", "rect": [20, 100, 200, 120], "image": photo, "src": "/menu/a.jpg"})
    (directory / "page-0.dom.json").write_text(json.dumps(dom))
    capture = Capture("page-0", directory / "page-0.png", directory / "page-0.dom.json", (480, 300), (0, 0), [])
    rendering = Rendering(engine, "menu", directory, {"page-0": capture}, {"engine": {"engine": engine}})
    rendering.write(device)
    return rendering


def test_store_and_compare(tmp_path):
    store = CaptureStore(tmp_path / "baselines", "rk3568", "servoshell")
    store.put(_rendering(tmp_path / "old"), reason="first")
    assert store.index("menu")["reason"] == "first"
    expected = store.captures("menu")
    same = compare_captures(expected, _rendering(tmp_path / "same"), store_preset(), tmp_path / "a", ("old", "new"))
    assert same.matches
    (tmp_path / "b").mkdir()
    changed = compare_captures(
        expected, _rendering(tmp_path / "new", heading="Menu!"), store_preset(), tmp_path / "b", ("old", "new")
    )
    assert not changed.matches
    report = json.loads((tmp_path / "b" / "diff.json").read_text())
    entry = report["captures"]["page-0"]
    assert entry["regions"][0]["elements"][0]["path"] == "body>h1"
    assert (tmp_path / "b" / entry["compare"]).is_file()
    assert (tmp_path / "b" / entry["regions"][0]["crop"]).is_file()
    assert "page-0" in changed.message("the baseline")


def store_preset():
    from servo_embedder_tests.diffing import BASELINE

    return BASELINE


def test_a_region_names_the_image_that_did_not_load(tmp_path):
    store = CaptureStore(tmp_path / "baselines", "rk3568", "servoshell")
    store.put(_rendering(tmp_path / "old", photo="loaded"))
    changed = compare_captures(
        store.captures("menu"), _rendering(tmp_path / "new", photo="broken"), store_preset(), tmp_path / "b", ("a", "b")
    )
    entry = changed.report["captures"]["page-0"]
    assert entry["regions"][0]["reading"].startswith("image /menu/a.jpg did not load in b; ")
    assert entry["dom_changes"][0]["image"] == ["loaded", "broken"]


def test_the_report_shows_differences_with_the_triage(tmp_path):
    artifacts = tmp_path / "artifacts"
    store = CaptureStore(tmp_path / "baselines", "rk3568", "servoshell")
    store.put(_rendering(tmp_path / "old"))
    rendering = _rendering(artifacts / "page-scenarios_servoshell_menu", heading="Menu!")
    key = f"{cli.BASELINE_TEST}menu]"
    folder = artifacts / "baseline-menu"
    folder.mkdir()
    (folder / TEST_ARTIFACTS_MARKER).write_text(key + "\n")
    compare_captures(store.captures("menu"), rendering, store_preset(), folder, ("baseline", "servoshell"))
    answer = {"crop": "regions/page-0-0.png", "main": "an exclamation mark <added>", "verdict": "neutral"}
    (folder / "vision.json").write_text(json.dumps({"model": "m", "answer": {"regions": [answer]}}))
    triage = {"agent": "a", "verdict": "intended", "summary": "new heading", "regions": {"page-0-0": "added text"}}
    (folder / "triage.json").write_text(json.dumps(triage))
    result = {"key": key, "status": "FAIL", "expected": ["PASS"], "message": "1 capture(s) differ"}
    (artifacts / "results.json").write_text(json.dumps({"device_class": "openharmony", "results": {key: result}}))
    assert cli.main(["report", str(artifacts)]) == 0
    page = (artifacts / "report.html").read_text()
    assert 'src="baseline-menu/page-0-expected.png"' in page and 'class="box"' in page
    assert "an exclamation mark &lt;added&gt;" in page and "added text" in page and "new heading" in page
    assert '<span class="status FAIL unexpected">' in page
    assert cli.main(["report", str(tmp_path / "baselines")]) == 1


def test_accept_stores_the_failed_scenarios(tmp_path, capsys):
    artifacts = tmp_path / "artifacts"
    _rendering(artifacts / "page-scenarios_servoshell_menu", heading="New")
    key = f"{cli.BASELINE_TEST}menu]"
    results = {"device_class": "openharmony", "results": {key: {"key": key, "status": "FAIL", "expected": ["PASS"]}}}
    (artifacts / "results.json").write_text(json.dumps(results))
    assert cli.main(["accept", str(artifacts), "--baselines", str(tmp_path / "baselines"), "--reason", "better"]) == 0
    store = CaptureStore(tmp_path / "baselines", "rk3568", "servoshell")
    assert list(store.captures("menu")) == ["page-0"]
    assert store.index("menu")["reason"] == "better"
    assert "stored 1 capture(s) of menu" in capsys.readouterr().out


def test_arkweb_engine_starts_its_ability_with_parameters(tmp_path):
    commands = []
    device = SimpleNamespace(shell=lambda command: commands.append(command) or "start ability successfully.")
    engine = ArkWebEngine(
        cast(OhosDevice, device), "com.example.viewer", None, tmp_path / "cd.log", "MainAbility", [["chrome", "none"]]
    )
    engine._start_app("http://127.0.0.1:8000/a.html")
    assert commands == [
        "aa start -a MainAbility -b com.example.viewer -U http://127.0.0.1:8000/a.html --ps chrome none"
    ]
