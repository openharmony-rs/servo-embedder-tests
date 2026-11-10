from typing import cast

from PIL import Image

from servo_embedder_tests.platform import OhosDevice
from servo_embedder_tests.references import ArkWebViewer, ReferenceStore, Rendered


class _Device:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def shell(self, command: str) -> str:
        self.commands.append(command)
        return "start ability successfully."

    def pid_of(self, bundle: str) -> int:
        return 1

    def has_focus(self, pid: int) -> bool:
        return True


def test_viewer_starts_its_ability_with_parameters():
    device = _Device()
    viewer = ArkWebViewer(cast(OhosDevice, device), "com.example.viewer", "MainAbility", [["chrome", "none"]])
    viewer.app.start("http://127.0.0.1:8000/a.html", None, parameters=viewer.parameters)
    assert device.commands == [
        "aa start -a MainAbility -b com.example.viewer -U http://127.0.0.1:8000/a.html --ps chrome none"
    ]


def test_store_keeps_images_and_how_they_were_captured(tmp_path):
    image = tmp_path / "capture.png"
    Image.new("RGB", (3, 2), "red").save(image)
    store = ReferenceStore(tmp_path / "references" / "rk3568")
    assert store.get("a") is None and store.info("a") == {}
    store.put("b", image, {"dpr": 1.5})
    store.put("a", image, {"dpr": 2.0})
    assert store.get("a") == tmp_path / "references" / "rk3568" / "a.png"
    assert ReferenceStore(store.directory).info("b") == {"dpr": 1.5}
    with Image.open(store.get("b")) as kept:
        assert kept.size == (3, 2)


def test_rendered_box_is_in_screen_pixels():
    rendered = Rendered((0, 10, 350, 100.4), 1.5, "ArkWeb/7.0")
    assert rendered.size == (350, 100)
    assert rendered.box((0, 120)) == (0, 135, 525, 286)
