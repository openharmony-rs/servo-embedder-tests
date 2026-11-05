from typing import cast

import pytest

from servo_embedder_tests.platform import HilogStream, OhosDevice


def test_hilog_text_since_mark(tmp_path):
    path = tmp_path / "hilog.txt"
    path.write_text("日志" * 100 + "\n", encoding="utf-8")
    stream = HilogStream(cast(OhosDevice, None), path)
    mark = stream.mark()
    with path.open("a", encoding="utf-8") as f:
        f.write("panic\n")
    assert stream.text(mark) == "panic\n"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"const.product.os.dist.name": "HarmonyOS", "ohos.boot.hardware": "kirin9000"}, "harmonyos"),
        ({"const.product.os.dist.name": "OpenHarmony", "ohos.boot.hardware": "rk3568"}, "openharmony"),
        ({"const.product.os.dist.name": "OpenHarmony", "ohos.boot.hardware": "virt"}, "openharmony-emulator"),
        ({}, "openharmony"),
    ],
)
def test_device_class(params, expected):
    device = OhosDevice.__new__(OhosDevice)
    device.param = params.get  # type: ignore[method-assign]
    assert device.device_class() == expected
