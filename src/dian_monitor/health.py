"""Dead-man's switch pings (healthchecks.io or compatible).

The monitor pings while it is alive; if pings stop, the service alerts you. That covers the
one failure the bot cannot report itself: the process or the machine being down.
The URL holds a secret key, so it is never logged. Pinging never raises.
"""
import time

import requests

MIN_GAP_SECONDS = 60


class Health:
    def __init__(self, url, session=None, monotonic=time.monotonic):
        self.url = url.rstrip("/")
        self.s = session or requests.Session()
        self.monotonic = monotonic
        self.last = None

    def ping(self, status=""):
        """status: "" (alive), "fail" or "start". Plain pings are sent at most once a minute."""
        if not self.url:
            return
        now = self.monotonic()
        if not status and self.last is not None and now - self.last < MIN_GAP_SECONDS:
            return
        try:
            self.s.get(f"{self.url}/{status}" if status else self.url, timeout=10)
            self.last = now
        except requests.RequestException:
            pass
