# Copyright 2025 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

"""mitmproxy record/replay for scenarios that load real sites; its options are ported from Servo's etc/ci/scenario."""

from __future__ import annotations

import enum
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .errors import ScenarioError

MITMPROXY_VERSION = "12.2.1"


class ProxyMode(enum.StrEnum):
    OFF = "off"
    REPLAY = "replay"
    RECORD = "record"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def mitmdump_command() -> list[str]:
    if path := shutil.which("mitmdump"):
        return [path]
    if uvx := shutil.which("uvx"):
        return [uvx, "--from", f"mitmproxy=={MITMPROXY_VERSION}", "mitmdump"]
    raise ScenarioError("mitmdump not found: install mitmproxy or uv")


class MitmProxy:
    def __init__(self, mode: ProxyMode, dump: Path, log_path: Path | None = None) -> None:
        self.mode = mode
        self.dump = dump
        self.log_path = log_path
        self.port = _free_port()
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> MitmProxy:
        command = [*mitmdump_command(), "--listen-host", "127.0.0.1", "-p", str(self.port)]
        if self.mode == ProxyMode.REPLAY:
            command += [
                "--server-replay",
                str(self.dump),
                # Requests missing from the dump get a 404 instead of reaching the network.
                "--set",
                "server_replay_extra=404",
                "--set",
                "server_replay_reuse=true",
                # Match requests without their body: analytics POSTs carry timestamps and would never match.
                "--set",
                "server_replay_ignore_content=true",
                # Don't connect upstream eagerly: a stalled connect would leave the page waiting on a resource.
                "--set",
                "connection_strategy=lazy",
            ]
        elif self.mode == ProxyMode.RECORD:
            self.dump.parent.mkdir(parents=True, exist_ok=True)
            # Strip conditional headers so the dump holds full responses, never 304s.
            command += ["-w", str(self.dump), "--set", "ssl_insecure=true", "--set", "anticache=true"]
        else:
            raise ValueError(f"no proxy needed for mode {self.mode}")
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        try:
            self._process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        finally:
            if self.log_path:
                log.close()  # type: ignore[union-attr]
        deadline = time.monotonic() + 60
        while True:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return self
            except OSError:
                if self._process.poll() is not None or time.monotonic() > deadline:
                    raise ScenarioError(f"mitmdump did not start (see {self.log_path})") from None
                time.sleep(0.3)

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None

    def servoshell_args(self) -> tuple[dict[str, str | bool], list[str]]:
        """Prefs and arguments for servoshell. The HTTP cache is off so that recording and replay see every request,
        whatever the app has cached from earlier runs."""
        uri = f"http://127.0.0.1:{self.port}"
        prefs: dict[str, str | bool] = {
            "network_http_proxy_uri": uri,
            "network_https_proxy_uri": uri,
            "network_http_cache_disabled": True,
        }
        return prefs, ["--ignore-certificate-errors"]
