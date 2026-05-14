"""Lifecycle management for the local SpiderFoot subprocess.

We treat SpiderFoot as a black-box web service: my app starts it, polls
it for health, talks to it via HTTP, and stops it on shutdown. The user
clones SpiderFoot into ./spiderfoot/ (or sets SPIDERFOOT_PATH) and we
launch its `sf.py` from there.

Why subprocess and not import?  SpiderFoot is a full CherryPy
application designed to run as its own process. Importing it as a
library is brittle across versions.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests


DEFAULT_PORT = 5001
DEFAULT_HOST = "127.0.0.1"
STARTUP_TIMEOUT_S = 60


@dataclass
class ManagerState:
    sf_path: Optional[str] = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    pid: Optional[int] = None
    started_at: Optional[float] = None
    last_error: Optional[str] = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def installed(self) -> bool:
        return bool(self.sf_path)


def _find_spiderfoot() -> Optional[str]:
    """Locate a SpiderFoot install."""
    env_path = os.environ.get("SPIDERFOOT_PATH")
    candidates = [
        env_path,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "spiderfoot"),
        os.path.expanduser("~/spiderfoot"),
        "/opt/spiderfoot",
    ]
    for c in candidates:
        if not c:
            continue
        sf_py = os.path.join(c, "sf.py")
        if os.path.isfile(sf_py):
            return c
    return None


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect((host, port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


class SpiderFootManager:
    """Singleton-ish manager. Starts SF on first use, stops on exit."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.state = ManagerState(
            sf_path=_find_spiderfoot(), host=host, port=port,
        )
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        atexit.register(self.stop)

    # --- introspection -------------------------------------------

    def status(self) -> dict:
        return {
            "installed": self.state.installed,
            "sf_path": self.state.sf_path,
            "host": self.state.host,
            "port": self.state.port,
            "base_url": self.state.base_url,
            "pid": self.state.pid,
            "started_at": self.state.started_at,
            "running": self.is_running(),
            "reachable": self.is_reachable(),
            "last_error": self.state.last_error,
        }

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def is_reachable(self) -> bool:
        try:
            r = requests.get(self.state.base_url, timeout=2)
            return r.status_code < 500
        except requests.RequestException:
            return False

    # --- lifecycle -----------------------------------------------

    def ensure_started(self) -> tuple[bool, str]:
        """Start SpiderFoot if it isn't already reachable.

        Returns (ok, message).
        """
        with self._lock:
            if self.is_reachable():
                return True, "Already reachable."
            if not self.state.installed:
                msg = "SpiderFoot is not installed."
                self.state.last_error = msg
                return False, msg
            if _port_open(self.state.host, self.state.port):
                # Something else is on this port already
                msg = f"Port {self.state.port} is in use by another process."
                self.state.last_error = msg
                return False, msg
            return self._spawn()

    def _spawn(self) -> tuple[bool, str]:
        assert self.state.sf_path
        cmd = [
            sys.executable, "sf.py",
            "-l", f"{self.state.host}:{self.state.port}",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=self.state.sf_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            self.state.last_error = f"Failed to spawn SpiderFoot: {e}"
            return False, self.state.last_error

        self.state.pid = self._proc.pid
        self.state.started_at = time.time()

        # Wait for it to start listening
        deadline = time.time() + STARTUP_TIMEOUT_S
        while time.time() < deadline:
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() or b"").decode("utf-8", "replace")
                self.state.last_error = (
                    f"SpiderFoot exited with code {self._proc.returncode}.\n"
                    f"{err[-1000:]}"
                )
                self._proc = None
                return False, self.state.last_error
            if self.is_reachable():
                self.state.last_error = None
                return True, "Started."
            time.sleep(0.5)

        self.state.last_error = (
            f"SpiderFoot didn't become reachable within "
            f"{STARTUP_TIMEOUT_S}s."
        )
        return False, self.state.last_error

    def stop(self) -> None:
        if not self.is_running():
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)  # type: ignore
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()  # type: ignore
            except Exception:
                pass
        self._proc = None
        self.state.pid = None


# --- module-level singleton ---------------------------------------


manager = SpiderFootManager(
    host=os.environ.get("SPIDERFOOT_HOST", DEFAULT_HOST),
    port=int(os.environ.get("SPIDERFOOT_PORT", str(DEFAULT_PORT))),
)


def setup_instructions(sf_path_hint: str) -> list[str]:
    """Human-readable steps shown when SF isn't installed."""
    return [
        f"cd \"{sf_path_hint}\"",
        "git clone https://github.com/smicallef/spiderfoot.git",
        "cd spiderfoot",
        "pip install -r requirements.txt",
        "cd ..",
        "# Restart this app; it will find SpiderFoot automatically.",
    ]
