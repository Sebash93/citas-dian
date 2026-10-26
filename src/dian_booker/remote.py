"""A phone-sized screen for Chrome that a human can reach from anywhere by URL.

Xvfb (virtual screen) -> x11vnc (VNC, password) -> websockify + noVNC (VNC in a web page)
-> cloudflared quick tunnel (public https URL). The packages are installed in the
background at the start of each workflow run (see monitor.yml), which touches
BOOKER_READY_FILE when done.
"""
import os
import re
import secrets
import socket
import subprocess
import threading
import time

TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
NOVNC_DIRS = ("/usr/share/novnc", "/usr/share/webapps/novnc", "/opt/homebrew/share/novnc")


def tunnel_url_in(line):
    m = TUNNEL_RE.search(line)
    return m.group(0) if m else None


def novnc_url(base, password):
    # noVNC reads these from the query string: connect at once, fit the phone screen.
    return f"{base}/vnc.html?autoconnect=1&resize=scale&reconnect=1&password={password}"


def _wait_port(port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"port {port} did not open in {timeout} s")


class LocalDisplay:
    """Use this machine's own screen (for runs on your computer). No link."""

    env = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def url(self, timeout=0):
        return None


class RemoteDisplay:
    def __init__(self, width, height, log, display=":99", vnc_port=5900, web_port=6080):
        self.width, self.height, self.log = width, height, log
        self.display, self.vnc_port, self.web_port = display, vnc_port, web_port
        self.password = secrets.token_hex(4)  # VNC passwords are capped at 8 characters
        self.env = {"DISPLAY": display}
        self.procs = []
        self._base = None
        self._ready = threading.Event()

    def __enter__(self):
        self._wait_installed()
        self._spawn(["Xvfb", self.display, "-screen", "0", f"{self.width}x{self.height}x24",
                     "-nolisten", "tcp"])
        sock = f"/tmp/.X11-unix/X{self.display.lstrip(':')}"
        for _ in range(50):
            if os.path.exists(sock):
                break
            time.sleep(0.1)
        self._spawn(["x11vnc", "-display", self.display, "-rfbport", str(self.vnc_port),
                     "-localhost", "-forever", "-shared", "-passwd", self.password, "-quiet"])
        _wait_port(self.vnc_port, 15)
        web = next((d for d in NOVNC_DIRS if os.path.isdir(d)), NOVNC_DIRS[0])
        self._spawn(["websockify", "--web", web, str(self.web_port), f"localhost:{self.vnc_port}"])
        _wait_port(self.web_port, 15)
        tunnel = self._spawn(["cloudflared", "tunnel", "--no-autoupdate", "--url",
                              f"http://localhost:{self.web_port}"], stderr=subprocess.PIPE)
        threading.Thread(target=self._read_tunnel, args=(tunnel,), daemon=True).start()
        return self

    def __exit__(self, *exc):
        for proc in reversed(self.procs):
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return False

    def url(self, timeout=60):
        """The noVNC link, once the tunnel is up (blocks up to `timeout` seconds)."""
        if not self._ready.wait(timeout):
            raise RuntimeError("cloudflared did not report a tunnel URL")
        return novnc_url(self._base, self.password)

    def _wait_installed(self, timeout=240):
        flag = os.environ.get("BOOKER_READY_FILE")
        if not flag:
            return
        deadline = time.monotonic() + timeout
        while not os.path.exists(flag):
            if time.monotonic() > deadline:
                raise RuntimeError("display packages were not installed in time")
            time.sleep(1)

    def _spawn(self, cmd, stderr=subprocess.DEVNULL):
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr, text=True)
        self.procs.append(proc)
        return proc

    def _read_tunnel(self, proc):
        for line in proc.stderr:
            url = tunnel_url_in(line)
            if url and not self._ready.is_set():
                self._base = url
                self.log("Tunnel is up.")
                self._ready.set()
