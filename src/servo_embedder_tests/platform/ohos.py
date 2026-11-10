"""OpenHarmony / HarmonyOS devices, driven over hdc."""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from hdc_py import HarmonyDevice, HarmonyDevicePerfMode, Hdc

from ..errors import ScenarioError
from ..ui import Layout

DEVICE_TMP = "/data/local/tmp"
# Buttons that close system pop-ups such as the USB mode dialog without changing anything.
DIALOG_BUTTONS = ("OK", "确定", "Cancel", "取消")
# The system web engine's bundle: HarmonyOS, OpenHarmony.
ARKWEB_BUNDLES = ("com.huawei.hmos.arkwebcore", "com.ohos.arkwebcore")


@dataclass(frozen=True)
class WindowInfo:
    name: str
    pid: int
    window_id: int
    z_order: int
    rect: tuple[int, int, int, int]


class OhosDevice:
    """One hdc target. Thin layer over hdc-py with the helpers scenarios need."""

    family = "ohos"

    def __init__(self, target: str | None = None, hdc_path: str | None = None) -> None:
        hdc = Hdc(hdc_path=Path(hdc_path) if hdc_path else None)
        self.device: HarmonyDevice = hdc.connect(target=target)
        self.target = self.device.target
        self.hdc_path = str(self.device.hdc_path)
        self._forwards: list[tuple[str, str, str]] = []

    def __repr__(self) -> str:
        return f"OhosDevice({self.target!r})"

    def hdc(self, *args: str, timeout: float = 60, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.hdc_path, "-t", self.target, *args], capture_output=True, text=True, timeout=timeout, check=check
        )

    def shell(self, command: str, timeout: float = 60) -> str:
        """Runs `command` and returns its output. Its exit code is unknown; use `checked_shell` when it matters."""
        return self.device.cmd_without_exit_code(command, timeout=timeout).output

    def checked_shell(self, command: str, timeout: float = 60) -> str:
        return str(self.device.cmd(command, capture_output=True, text=True, timeout=timeout).stdout)

    def param(self, name: str) -> str | None:
        value = self.shell(f"param get {shlex.quote(name)}").strip()
        if not value or "fail!" in value:
            return None
        return value

    def device_class(self) -> str:
        if self.param("const.product.os.dist.name") == "HarmonyOS":
            return "harmonyos"
        # The QEMU images, e.g. those of `cargo ohos emulator`, run on QEMU's `virt` machine.
        if self.param("ohos.boot.hardware") == "virt":
            return "openharmony-emulator"
        return "openharmony"

    def name(self) -> str:
        """The device's model, or its hardware on boards that keep OpenHarmony's default model name, e.g. rk3568."""
        model = self.param("const.product.model")
        if model in (None, "ohos"):
            model = self.param("ohos.boot.hardware")
        return model or self.target

    def describe(self) -> dict[str, str | None]:
        return {
            "os": self.param("const.ohos.fullname"),
            "api": self.param("const.ohos.apiversion"),
            "model": self.param("const.product.model"),
            "arkweb": next(filter(None, map(self.bundle_version, ARKWEB_BUNDLES)), None),
        }

    def bundle_version(self, bundle: str) -> str | None:
        """The version name of an installed bundle."""
        m = re.search(r'"versionName": "([^"]+)"', self.shell(f"bm dump -n {shlex.quote(bundle)}"))
        return m.group(1) if m else None

    def time_ms(self) -> int:
        """The device's clock, in milliseconds since the epoch, the time base of `faults`."""
        return int(self.shell("date +%s").strip()) * 1000

    def faults(self, since_ms: int) -> list[dict[str, object]]:
        """The fault events, such as crashes and freezes, that the system logged since `since_ms`."""
        output = self.shell(f"hisysevent -l -g FAULT -s {int(since_ms)} -e {self.time_ms() + 1000}")
        events = []
        for line in output.splitlines():
            with contextlib.suppress(ValueError):
                events.append(json.loads(line))
        return events

    def forward(self, port: int) -> None:
        """Makes `port` on the device reachable as 127.0.0.1:`port` on the host."""
        self.device.forward_port(f"tcp:{port}", f"tcp:{port}")
        self._forwards.append(("fport", f"tcp:{port}", f"tcp:{port}"))

    def reverse(self, port: int) -> None:
        """Makes 127.0.0.1:`port` on the host reachable as 127.0.0.1:`port` on the device."""
        self.device.reverse_port(f"tcp:{port}", f"tcp:{port}")
        self._forwards.append(("rport", f"tcp:{port}", f"tcp:{port}"))

    def remove_forwards(self) -> None:
        for _, first, second in reversed(self._forwards):
            self.device.remove_port_forward(first, second)
        self._forwards.clear()

    def install(self, hap: Path, bundle: str) -> None:
        output = self.hdc("install", "-r", str(hap), timeout=300).stdout
        if "install bundle successfully" not in output:
            raise ScenarioError(f"Installing {hap} failed: {output.strip()}")
        if bundle not in self.shell("bm dump -a"):
            raise ScenarioError(f"{bundle} is not installed after installing {hap}")

    def is_installed(self, bundle: str) -> bool:
        return any(line.strip() == bundle for line in self.shell("bm dump -a").splitlines())

    def pid_of(self, bundle: str) -> int | None:
        output = self.shell(f"pidof {shlex.quote(bundle)}").split()
        return int(output[0]) if output and output[0].isdigit() else None

    def tap(self, x: int, y: int) -> None:
        self.shell(f"uinput -T -c {int(x)} {int(y)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        self.shell(f"uinput -T -m {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 800, hold_ms: int = 400) -> None:
        """Like `swipe`, but holds still for `hold_ms` before lifting, so that the content doesn't fling."""
        self.shell(f"uinput -T -m {int(x1)} {int(y1)} {int(x2)} {int(y2)} -k {int(hold_ms)} {int(duration_ms)}")

    def key(self, name: str) -> None:
        self.shell(f"uitest uiInput keyEvent {shlex.quote(name)}")

    def screenshot(self, dest: Path) -> Path:
        """A screenshot as JPEG, or as PNG if `dest` ends with `.png`, which is lossless but slower."""
        if dest.suffix == ".png":
            device_path = f"{DEVICE_TMP}/servo-embedder-tests-screen.png"
            command = f"rm -f {device_path}; uitest screenCap -p {device_path}"
        else:
            device_path = f"{DEVICE_TMP}/servo-embedder-tests-screen.jpeg"
            command = f"rm -f {device_path}; snapshot_display -f {device_path}"
        self.shell(command)
        dest.unlink(missing_ok=True)
        self.hdc("file", "recv", device_path, str(dest))
        if not dest.is_file():
            raise ScenarioError(f"`{command.split('; ', 1)[1]}` produced no screenshot")
        return dest

    def dump_layout(self, dest: Path | None = None) -> Layout:
        device_path = f"{DEVICE_TMP}/servo-embedder-tests-layout.json"
        self.shell(f"rm -f {device_path}; uitest dumpLayout -p {device_path}", timeout=60)
        with contextlib.ExitStack() as stack:
            if dest is None:
                import tempfile

                dest = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "layout.json"
            dest.unlink(missing_ok=True)
            self.hdc("file", "recv", device_path, str(dest))
            if not dest.is_file():
                raise ScenarioError("uitest dumpLayout produced no layout")
            return Layout.from_file(dest)

    def windows(self) -> tuple[list[WindowInfo], int | None]:
        """Visible windows and the focused window id, from the window manager dump."""
        output = self.shell("hidumper -s WindowManagerService -a '-a'")
        windows = []
        focused = None
        row = re.compile(
            r"^(?P<name>\S+)\s+\d+\s+(?P<pid>\d+)\s+(?P<wid>\d+)\s+\d+\s+\d+\s+\d+\s+(?P<z>-?\d+)\s+\d+\s+"
            r"\[\s*(?P<x>-?\d+)\s+(?P<y>-?\d+)\s+(?P<w>\d+)\s+(?P<h>\d+)\s*\]"
        )
        for line in output.splitlines():
            if m := row.match(line):
                windows.append(
                    WindowInfo(
                        m["name"],
                        int(m["pid"]),
                        int(m["wid"]),
                        int(m["z"]),
                        (int(m["x"]), int(m["y"]), int(m["w"]), int(m["h"])),
                    )
                )
            elif line.lower().startswith("focus window:"):
                value = line.split(":", 1)[1].strip()
                focused = int(value) if value.isdigit() else None
        return windows, focused

    def is_screen_locked(self) -> bool:
        output = self.shell("hidumper -s ScreenlockService -a -all")
        if m := re.search(r"screenLocked\s+(true|false)", output):
            return m.group(1) == "true"
        windows, _ = self.windows()
        return any("ScreenLock" in w.name and w.z_order >= 0 for w in windows)

    def unlock(self) -> None:
        """Wakes the screen and swipes away a lock screen without credentials."""
        self.shell("power-shell wakeup")
        for _ in range(3):
            if not self.is_screen_locked():
                return
            width, height = self.screen_size()
            self.swipe(width // 2, height * 9 // 10, width // 2, height // 10, 300)
            time.sleep(1)
        if self.is_screen_locked():
            raise ScenarioError("The screen is still locked; remove the lock screen credential on the test device")

    def screen_size(self) -> tuple[int, int]:
        windows, _ = self.windows()
        sizes = [w.rect[2:] for w in windows if w.rect[0] == 0 and w.rect[1] == 0 and w.rect[2] and w.rect[3]]
        if sizes:
            return max(sizes, key=lambda s: s[0] * s[1])
        return 720, 1280

    def focused_window(self) -> WindowInfo | None:
        windows, focused = self.windows()
        return next((w for w in windows if w.window_id == focused), None)

    def has_focus(self, pid: int) -> bool:
        window = self.focused_window()
        return window is not None and window.pid == pid

    def ensure_focused(self, pid: int, attempts: int = 3) -> bool:
        """Dismisses overlays (lock screen, USB mode pop-up) until a window of `pid` has focus.

        HarmonyOS closes the USB pop-up on Back; OpenHarmony 7.0 needs its button tapped.
        """
        for attempt in range(attempts):
            if self.has_focus(pid):
                return True
            if self.is_screen_locked():
                self.unlock()
            elif attempt == 0:
                self.key("Back")
            else:
                self.press_dialog_button()
            time.sleep(1)
        return self.has_focus(pid)

    def press_dialog_button(self) -> bool:
        layout = self.dump_layout()
        for text in DIALOG_BUTTONS:
            if button := layout.find(type="Button", text=text):
                self.tap(*button.center)
                return True
        return False

    @contextlib.contextmanager
    def awake(self) -> Iterator[None]:
        """Keeps the screen on, in performance mode, for the duration of the block."""
        with HarmonyDevicePerfMode(hdc=self.device):
            self.shell('hidumper -s PowerManagerService -a "-t"')
            try:
                yield
            finally:
                self.shell('hidumper -s PowerManagerService -a "-f"')


class HilogStream:
    """Streams hilog into a file from the moment it's started; `wait_for` searches what arrived so far."""

    def __init__(self, device: OhosDevice, path: Path) -> None:
        self.device = device
        self.path = path
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._restore: list[str] = []

    def start(self, app_logs: bool = True) -> HilogStream:
        if app_logs:
            # HarmonyOS drops app logs under flow control unless it's off.
            for on, off in (("hilog -Q pidoff", "hilog -Q pidon"), ("hilog -Q domainoff", "hilog -Q domainon")):
                self.device.shell(on)
                self._restore.append(off)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.path, "wb")
        self._process = subprocess.Popen(
            [self.device.hdc_path, "-t", self.device.target, "shell", "hilog"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        return self

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        for command in self._restore:
            self.device.shell(command)
        self._restore.clear()

    def text(self, since: int = 0) -> str:
        """The log from `since`, a `mark()`, to its current end."""
        try:
            with open(self.path, "rb") as f:
                f.seek(since)
                return f.read().decode(errors="replace")
        except FileNotFoundError:
            return ""

    def mark(self) -> int:
        """Current end of the log, to pass as `since` to `wait_for`."""
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def wait_for(self, pattern: str | re.Pattern[str], timeout: float = 15, since: int = 0) -> re.Match[str] | None:
        regex = re.compile(pattern) if isinstance(pattern, str) else pattern
        deadline = time.monotonic() + timeout
        while True:
            with open(self.path, "rb") as f:
                f.seek(since)
                if m := regex.search(f.read().decode(errors="replace")):
                    return m
            if time.monotonic() > deadline:
                return None
            time.sleep(0.2)
