from pathlib import Path

import pytest
import tomlkit

from servo_embedder_tests.expectations import Expectations, Result, RunReport, lookup_chain, update


def load(text: str) -> Expectations:
    return Expectations(tomlkit.parse(text))


def test_missing_entry_is_pass():
    assert load("").get("a.py::t", "harmonyos").statuses == ["PASS"]


def test_lookup_falls_back_through_prefixes():
    assert lookup_chain("openharmony-emulator") == ["openharmony-emulator", "openharmony", "default"]
    e = load('["a.py::t"]\ndefault = "FAIL"\nopenharmony = ["PASS", "TIMEOUT"]\nissue = "x"\n')
    assert e.get("a.py::t", "openharmony-emulator").statuses == ["PASS", "TIMEOUT"]
    assert e.get("a.py::t", "harmonyos").statuses == ["FAIL"]
    assert e.get("a.py::t", "harmonyos").issue == "x"


def test_rejects_unknown_status():
    with pytest.raises(ValueError):
        load('["a.py::t"]\ndefault = "BROKEN"\n')


def report(device_class: str, **statuses: str) -> RunReport:
    r = RunReport(device_class)
    for key, status in statuses.items():
        r.results[key] = Result(key, status, ["PASS"])
    return r


def test_update_records_new_failure_and_keeps_comments(tmp_path: Path):
    path = tmp_path / "e.toml"
    path.write_text('# keep me\n["b"]\ndefault = "FAIL"  # tracked\nissue = "https://example.org/1"\n')
    e = Expectations.load(path)
    changes = update(e, [report("harmonyos", a="FAIL", b="FAIL")])
    e.save()
    text = path.read_text()
    assert "# keep me" in text and "# tracked" in text
    assert changes == ["a [harmonyos]: PASS -> FAIL"]
    assert Expectations.load(path).get("a", "harmonyos").statuses == ["FAIL"]
    assert Expectations.load(path).get("a", "linux").statuses == ["PASS"]


def test_update_removes_entry_when_test_passes_again():
    e = load('["a"]\nharmonyos = "FAIL"\n')
    update(e, [report("harmonyos", a="PASS")])
    assert e.keys() == []


def test_update_keeps_issue_link_and_drops_redundant_class():
    e = load('["a"]\ndefault = "FAIL"\nharmonyos = "PASS"\nissue = "i"\n')
    update(e, [report("harmonyos", a="FAIL")])
    assert dict(e.document["a"]) == {"default": "FAIL", "issue": "i"}


def test_update_keeps_entries_and_comments_in_place():
    e = load('["a"]\n# flaky\nharmonyos = ["PASS", "TIMEOUT"]\nopenharmony = "FAIL"\nissue = "i"\n')
    update(e, [report("openharmony", a="PASS")])
    assert tomlkit.dumps(e.document) == '["a"]\n# flaky\nharmonyos = ["PASS", "TIMEOUT"]\nissue = "i"\n'


def test_update_intermittent_unions_statuses():
    e = load('["a"]\ndefault = "FAIL"\n')
    update(e, [report("linux", a="PASS")], intermittent=True)
    assert e.get("a", "linux").statuses == ["PASS", "FAIL"]


def test_update_from_several_runs_of_one_class_marks_flaky():
    e = load("")
    update(e, [report("linux", a="PASS"), report("linux", a="TIMEOUT")])
    assert e.get("a", "linux").statuses == ["PASS", "TIMEOUT"]


def test_update_prune():
    e = load('["gone"]\ndefault = "FAIL"\n')
    update(e, [report("linux", a="PASS")], prune=True)
    assert e.keys() == []


def test_report_round_trip(tmp_path: Path):
    r = report("linux", a="CRASH")
    path = tmp_path / "r.json"
    path.write_text(r.to_json())
    assert RunReport.from_file(path).results["a"].status == "CRASH"
