"""WebDriver sessions against servoshell."""

from __future__ import annotations

import socket
import time
from collections.abc import Callable

from selenium import webdriver
from selenium.webdriver.common.options import ArgOptions
from selenium.webdriver.remote.client_config import ClientConfig
from selenium.webdriver.remote.remote_connection import RemoteConnection

from .errors import ScenarioError


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.settimeout(1)
            # hdc's forward accepts the connection even when nothing listens on the device; the device side
            # then closes it. A real server keeps it open.
            try:
                return sock.recv(1, socket.MSG_PEEK) != b""
            except TimeoutError:
                return True
    except OSError:
        return False


def connect(
    port: int, timeout: float = 60, grace: float = 1.0, alive: Callable[[], bool] | None = None
) -> webdriver.Remote:
    """Opens a session on servoshell's WebDriver server on 127.0.0.1:`port`.

    Servo's WebDriver server checks the `Host` header, so the host port must equal the port servoshell listens on.
    Creating a session before servoshell has a window panics servoshell, hence the `grace` period after the server
    starts accepting connections.
    """
    deadline = time.monotonic() + timeout
    next_alive_check = time.monotonic() + 2
    while not _port_open(port):
        if time.monotonic() > deadline:
            raise ScenarioError(f"WebDriver server on port {port} did not come up within {timeout}s")
        if alive is not None and time.monotonic() > next_alive_check:
            if not alive():
                raise ScenarioError("servoshell exited before its WebDriver server came up")
            next_alive_check = time.monotonic() + 2
        time.sleep(0.3)
    time.sleep(grace)
    options = ArgOptions()
    options.set_capability("browserName", "servo")
    config = ClientConfig(remote_server_addr=f"http://127.0.0.1:{port}", timeout=120)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return webdriver.Remote(command_executor=RemoteConnection(client_config=config), options=options)
        except Exception as error:
            last_error = error
            time.sleep(0.5)
    raise ScenarioError(f"Could not create a WebDriver session on port {port}: {last_error}")
