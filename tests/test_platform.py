from typing import cast

from servo_embedder_tests.platform import HilogStream, OhosDevice


def test_hilog_text_since_mark(tmp_path):
    path = tmp_path / "hilog.txt"
    path.write_text("日志" * 100 + "\n", encoding="utf-8")
    stream = HilogStream(cast(OhosDevice, None), path)
    mark = stream.mark()
    with path.open("a", encoding="utf-8") as f:
        f.write("panic\n")
    assert stream.text(mark) == "panic\n"
