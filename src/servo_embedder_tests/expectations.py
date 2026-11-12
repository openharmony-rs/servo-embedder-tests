"""WPT-style expectations: per test and device class, the statuses a run may produce.

The file is TOML. Each table is a test key (see `test_key`); each entry maps a device class, or `default`, to a status
or a list of statuses. Lookup tries the device class, then its prefixes up to each `-` (so `openharmony-emulator`
falls back to `openharmony`), then `default`. A test without an entry is expected to PASS.

    ["navigation/test_history.py::test_back[webdriver]"]
    default = "PASS"
    harmonyos = ["PASS", "FAIL"]
    issue = "https://github.com/servo/servo/issues/12345"
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from tomlkit.items import Table

STATUSES = ("PASS", "FAIL", "TIMEOUT", "CRASH", "SKIP")
NON_STATUS_KEYS = ("issue", "comment")


def _as_list(value: object) -> list[str]:
    values = [value] if isinstance(value, str) else list(value)  # type: ignore[call-overload]
    for status in values:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
    return [str(v) for v in values]


def lookup_chain(device_class: str) -> list[str]:
    parts = device_class.split("-")
    return ["-".join(parts[:i]) for i in range(len(parts), 0, -1)] + ["default"]


@dataclass
class Expectation:
    statuses: list[str]
    issue: str | None = None

    @property
    def is_default(self) -> bool:
        return self.statuses == ["PASS"]


class Expectations:
    def __init__(self, document: tomlkit.TOMLDocument | None = None, path: Path | None = None) -> None:
        self.document = document if document is not None else tomlkit.document()
        self.path = path
        for key, table in self.document.items():
            if not isinstance(table, dict):
                raise ValueError(f"{path}: entry {key!r} must be a table")
            for name, value in table.items():
                if name not in NON_STATUS_KEYS:
                    _as_list(value)

    @classmethod
    def load(cls, path: Path | None) -> Expectations:
        if path is None or not path.exists():
            return cls(path=path)
        return cls(tomlkit.parse(path.read_text(encoding="utf-8")), path)

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("no path to save expectations to")
        target.write_text(tomlkit.dumps(self.document), encoding="utf-8")

    def get(self, key: str, device_class: str) -> Expectation:
        table = self.document.get(key)
        if table is None:
            return Expectation(["PASS"])
        issue = table.get("issue")
        for name in lookup_chain(device_class):
            if name in table:
                return Expectation(_as_list(table[name]), issue)
        return Expectation(["PASS"], issue)

    def keys(self) -> list[str]:
        return list(self.document.keys())

    def set(self, key: str, device_class: str, statuses: list[str]) -> None:
        """Records `statuses` for `device_class`, then drops entries that the fallback chain already implies."""
        table = self.document.get(key)
        if table is None:
            table = tomlkit.table()
            self.document[key] = table
        assert isinstance(table, Table)
        table[device_class] = statuses[0] if len(statuses) == 1 else sorted(statuses, key=STATUSES.index)
        self._simplify(key)

    def _simplify(self, key: str) -> None:
        table = self.document[key]
        assert isinstance(table, Table)
        for name in [n for n in table.keys() if n not in NON_STATUS_KEYS and n != "default"]:
            fallback = next((_as_list(table[n]) for n in lookup_chain(name)[1:] if n in table), ["PASS"])
            if sorted(_as_list(table[name])) == sorted(fallback):
                del table[name]
        if "default" in table and _as_list(table["default"]) == ["PASS"]:
            only_default = all(n in NON_STATUS_KEYS or n == "default" for n in table.keys())
            if only_default:
                table.pop("default")
        if all(n in NON_STATUS_KEYS for n in table.keys()):
            del self.document[key]

    def remove(self, key: str) -> None:
        if key in self.document:
            del self.document[key]


@dataclass
class Result:
    key: str
    status: str
    expected: list[str]
    message: str = ""
    duration: float = 0.0

    @property
    def unexpected(self) -> bool:
        return self.status != "SKIP" and self.status not in self.expected


@dataclass
class RunReport:
    device_class: str
    results: dict[str, Result] = field(default_factory=dict)
    environment: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "device_class": self.device_class,
                "environment": self.environment,
                "results": {k: vars(r) for k, r in sorted(self.results.items())},
            },
            indent=2,
        )

    @classmethod
    def from_file(cls, path: Path) -> RunReport:
        data = json.loads(path.read_text())
        report = cls(data["device_class"], environment=data.get("environment", {}))
        for key, raw in data["results"].items():
            report.results[key] = Result(**raw)
        return report


def update(
    expectations: Expectations,
    reports: Iterable[RunReport],
    intermittent: bool = False,
    prune: bool = False,
) -> list[str]:
    """Rewrites expectations so the given runs would have no unexpected results. Returns a change log."""
    changes = []
    observed: dict[tuple[str, str], set[str]] = {}
    seen: set[str] = set()
    for report in reports:
        for key, result in report.results.items():
            seen.add(key)
            observed.setdefault((key, report.device_class), set()).add(result.status)
    for (key, device_class), statuses in sorted(observed.items()):
        if "SKIP" in statuses and len(statuses) == 1:
            continue
        statuses.discard("SKIP")
        current = expectations.get(key, device_class).statuses
        new = set(statuses) | (set(current) if intermittent else set())
        if new == set(current):
            continue
        expectations.set(key, device_class, sorted(new, key=STATUSES.index))
        changes.append(f"{key} [{device_class}]: {'/'.join(current)} -> {'/'.join(sorted(new, key=STATUSES.index))}")
    if prune:
        for key in expectations.keys():
            if key not in seen:
                expectations.remove(key)
                changes.append(f"{key}: removed (no longer collected)")
    return changes
