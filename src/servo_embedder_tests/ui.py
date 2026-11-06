"""Parser for `uitest dumpLayout` component trees."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Node:
    attributes: dict[str, str]
    children: list[Node] = field(default_factory=list)

    @property
    def type(self) -> str:
        return self.attributes.get("type", "")

    @property
    def text(self) -> str:
        return self.attributes.get("text", "")

    @property
    def id(self) -> str:
        return self.attributes.get("id", "") or self.attributes.get("key", "")

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """(left, top, right, bottom) in screen pixels."""
        numbers = [int(n) for n in re.findall(r"-?\d+", self.attributes.get("bounds", ""))]
        if len(numbers) != 4:
            return (0, 0, 0, 0)
        return (numbers[0], numbers[1], numbers[2], numbers[3])

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def is_visible(self) -> bool:
        left, top, right, bottom = self.bounds
        return right > left and bottom > top and self.attributes.get("visible", "true") != "false"

    @property
    def is_selected(self) -> bool:
        return self.attributes.get("selected") == "true"

    def walk(self) -> Iterator[Node]:
        yield self
        for child in self.children:
            yield from child.walk()


class Layout:
    def __init__(self, root: Node) -> None:
        self.root = root

    @classmethod
    def from_json(cls, data: dict) -> Layout:
        def build(raw: dict) -> Node:
            return Node(
                {k: str(v) for k, v in raw.get("attributes", {}).items()},
                [build(child) for child in raw.get("children", [])],
            )

        return cls(build(data))

    @classmethod
    def from_file(cls, path: Path) -> Layout:
        return cls.from_json(json.loads(path.read_text()))

    def find_all(
        self, *, type: str | None = None, text: str | None = None, id: str | None = None, visible: bool = True
    ) -> list[Node]:
        return [
            node
            for node in self.root.walk()
            if (type is None or node.type == type)
            and (text is None or node.text == text)
            and (id is None or node.id == id)
            and (not visible or node.is_visible)
        ]

    def find(self, **kwargs: str | bool | None) -> Node | None:
        found = self.find_all(**kwargs)  # type: ignore[arg-type]
        return found[0] if found else None

    def webview(self) -> Node | None:
        """The XComponent that shows the active webview: the largest visible one."""
        candidates = self.find_all(type="XComponent")
        if not candidates:
            return None

        def area(node: Node) -> int:
            left, top, right, bottom = node.bounds
            return (right - left) * (bottom - top)

        return max(candidates, key=area)
