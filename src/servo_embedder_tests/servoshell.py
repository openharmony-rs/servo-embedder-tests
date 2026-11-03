"""Launching and stopping servoshell, per platform."""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .errors import ScenarioError
from .platform import DesktopHost, OhosDevice

PrefValue = str | int | float | bool


def pref_arg(name: str, value: PrefValue) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    return f"--pref={name}={value}"


class ServoshellOhos:
    """servoshell as the `org.servo.servo` (or another bundle) HAP on an hdc device."""

    def __init__(self, device: OhosDevice, bundle: str = "org.servo.servo", ability: str = "EntryAbility") -> None:
        self.device = device
        self.bundle = bundle
        self.ability = ability
        self.pid: int | None = None

    def start(
        self,
        url: str,
        webdriver_port: int | None,
        prefs: Mapping[str, PrefValue] = {},
        args: Sequence[str] = (),
    ) -> None:
        flags = list(args) + [pref_arg(name, value) for name, value in prefs.items()]
        if webdriver_port is not None:
            flags.insert(0, f"--webdriver={webdriver_port}")
        # `--psn=` passes one token; servoshell's EntryAbility turns it back into a command-line flag.
        parts = ["aa", "start", "-a", self.ability, "-b", self.bundle, "-U", url]
        parts += [f"--psn={flag}" for flag in flags]
        command = shlex.join(parts)
        output = self.device.shell(command)
        if "successfully" not in output:
            raise ScenarioError(f"`aa start` failed: {output.strip()}")
        deadline = time.monotonic() + 15
        while (pid := self.device.pid_of(self.bundle)) is None:
            if time.monotonic() > deadline:
                raise ScenarioError(f"{self.bundle} has no process after `aa start`")
            time.sleep(0.3)
        self.pid = pid
        if self._wait_for_focus(pid, 20) or self.device.ensure_focused(pid):
            return
        # A pop-up in front can keep the ability from coming to the foreground at all.
        self.device.shell(command)
        if self._wait_for_focus(pid, 10) or self.device.ensure_focused(pid):
            return
        window = self.device.focused_window()
        has_window = any(w.pid == pid for w in self.device.windows()[0])
        raise ScenarioError(
            f"{self.bundle} is not in the foreground (focused window: {window.name if window else None}; "
            f"app window: {'yes' if has_window else 'none'}). Check hilog for the ability's start-up."
        )

    def _wait_for_focus(self, pid: int, timeout: float) -> bool:
        """Waits for the app's window; returns whether it has focus. Returns early if another window keeps it."""
        deadline = time.monotonic() + timeout
        seen_at = None
        while time.monotonic() < deadline:
            if self.device.has_focus(pid):
                return True
            if seen_at is None and any(w.pid == pid for w in self.device.windows()[0]):
                seen_at = time.monotonic()
            if seen_at is not None and time.monotonic() - seen_at > 2:
                return False
            time.sleep(0.4)
        return False

    def stop(self) -> None:
        self.device.shell(f"aa force-stop {shlex.quote(self.bundle)}")
        self.pid = None

    def is_running(self) -> bool:
        return self.pid is not None and self.device.pid_of(self.bundle) == self.pid


class ServoshellDesktop:
    """servoshell as a local binary."""

    def __init__(self, host: DesktopHost, binary: Path, log_path: Path | None = None) -> None:
        self.host = host
        self.binary = binary
        self.log_path = log_path
        self.process: subprocess.Popen[bytes] | None = None

    def start(
        self,
        url: str,
        webdriver_port: int | None,
        prefs: Mapping[str, PrefValue] = {},
        args: Sequence[str] = (),
    ) -> None:
        if not self.binary.is_file():
            raise ScenarioError(f"servoshell binary {self.binary} does not exist (pass --servo-bin)")
        command = [str(self.binary)]
        if webdriver_port is not None:
            command.append(f"--webdriver={webdriver_port}")
        command += list(args) + [pref_arg(name, value) for name, value in prefs.items()] + [url]
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        try:
            self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        finally:
            if self.log_path:
                log.close()  # type: ignore[union-attr]

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.process = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


Servoshell = ServoshellOhos | ServoshellDesktop
