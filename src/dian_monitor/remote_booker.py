"""Start the booker as a GitHub Actions run when the monitor runs elsewhere (Render).

The monitor dispatches `booker.yml` through the GitHub API with a fine-grained token
(this repo only, "Actions: read and write") in GITHUB_DISPATCH_TOKEN. After a real Medellín
booking, booker.yml uploads an empty `booking-booked` artifact; while it exists the monitor
starts no more bookers. It is checked on every alert (no local cache), so deleting the
artifact on GitHub re-enables booking at once.
Errors never include the token: they carry only the HTTP status and GitHub's message.
"""
import time

import requests

API = "https://api.github.com/repos/{repo}/actions/{path}"
WORKFLOW = "booker.yml"
BOOKED_ARTIFACT = "booking-booked"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
# A dispatched run takes a few seconds to show up in the API; count it as running meanwhile.
DISPATCH_GRACE_SECONDS = 90


class DispatchError(Exception):
    pass


class GitHubBooker:
    def __init__(self, repo, token, ref="main", session=None, monotonic=time.monotonic):
        self.repo = repo
        self.token = token
        self.ref = ref
        self.s = session or requests.Session()
        self.monotonic = monotonic
        self.dispatched_at = None
        self.last_run = None  # the active run seen by the latest running() call

    def _call(self, method, path, **kwargs):
        if not self.token:
            raise DispatchError("GITHUB_DISPATCH_TOKEN is not set")
        try:
            resp = self.s.request(method, API.format(repo=self.repo, path=path), timeout=20,
                                  headers={"Authorization": f"Bearer {self.token}",
                                           "Accept": "application/vnd.github+json",
                                           "X-GitHub-Api-Version": "2022-11-28"}, **kwargs)
        except requests.RequestException as e:
            raise DispatchError(f"GitHub {path}: {type(e).__name__}") from e
        if resp.status_code >= 300:
            try:
                detail = resp.json().get("message", "")
            except ValueError:
                detail = ""
            raise DispatchError(f"GitHub {path}: HTTP {resp.status_code} {detail}".strip())
        return resp.json() if resp.status_code != 204 else None

    def booked(self):
        """True while a `booking-booked` artifact (a real Medellín booking) exists."""
        body = self._call("GET", "artifacts", params={"name": BOOKED_ARTIFACT, "per_page": 1})
        return bool(body.get("total_count"))

    def running(self):
        if (self.dispatched_at is not None
                and self.monotonic() - self.dispatched_at < DISPATCH_GRACE_SECONDS):
            self.last_run = None
            return True
        body = self._call("GET", f"workflows/{WORKFLOW}/runs", params={"per_page": 5})
        self.last_run = next((run for run in body.get("workflow_runs", [])
                              if run.get("status") in ACTIVE), None)
        return self.last_run is not None

    def describe(self):
        """Short, stable text about the active run (logged only when it changes)."""
        if self.last_run is None:
            return "dispatched, not visible on GitHub yet"
        return f"GitHub run {self.last_run.get('id')} {self.last_run.get('status')}"

    def start(self, trigger):
        """Dispatch booker.yml unless a run is active. Raises DispatchError."""
        if self.running():
            return False
        self._call("POST", f"workflows/{WORKFLOW}/dispatches",
                   json={"ref": self.ref, "inputs": {"trigger": trigger}})
        self.dispatched_at = self.monotonic()
        return True

    def wait(self):
        pass  # the run lives on GitHub; nothing to wait for here
