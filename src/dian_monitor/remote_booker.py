"""Start the booker as a GitHub Actions run when the monitor runs elsewhere (Render).

The monitor dispatches `booker.yml` through the GitHub API with a fine-grained token
(this repo only, "Actions: read and write") in GITHUB_DISPATCH_TOKEN. After a real booking,
booker.yml uploads an empty `booking-booked` artifact; seeing it stops further dispatches.
Errors never include the token: they carry only the HTTP status and GitHub's message.
"""
import json
import os
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
    def __init__(self, repo, token, booking_file, ref="main", session=None,
                 monotonic=time.monotonic):
        self.repo = repo
        self.token = token
        self.booking_file = booking_file
        self.ref = ref
        self.s = session or requests.Session()
        self.monotonic = monotonic
        self.dispatched_at = None

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
        """True once booker.yml reported a real booking; remembered in the local booking file."""
        body = self._call("GET", "artifacts", params={"name": BOOKED_ARTIFACT, "per_page": 1})
        if not body.get("total_count"):
            return False
        os.makedirs(os.path.dirname(self.booking_file) or ".", exist_ok=True)
        with open(self.booking_file, "w", encoding="utf-8") as f:
            json.dump({"booked": True, "via": "github"}, f)
        return True

    def running(self):
        if (self.dispatched_at is not None
                and self.monotonic() - self.dispatched_at < DISPATCH_GRACE_SECONDS):
            return True
        body = self._call("GET", f"workflows/{WORKFLOW}/runs", params={"per_page": 5})
        return any(run.get("status") in ACTIVE for run in body.get("workflow_runs", []))

    def start(self, trigger):
        """Dispatch booker.yml unless already booked or a run is active. Raises DispatchError."""
        if self.booked() or self.running():
            return False
        self._call("POST", f"workflows/{WORKFLOW}/dispatches",
                   json={"ref": self.ref, "inputs": {"trigger": trigger}})
        self.dispatched_at = self.monotonic()
        return True

    def wait(self):
        pass  # the run lives on GitHub; nothing to wait for here
