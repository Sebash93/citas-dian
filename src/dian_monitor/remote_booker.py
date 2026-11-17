"""Start the booker as a GitHub Actions run when the monitor runs elsewhere (Render).

The monitor dispatches `booker.yml` through the GitHub API with a fine-grained token
(this repo only, "Actions: read and write") in GITHUB_DISPATCH_TOKEN, one run per case
(inputs `case` and `trigger`). Runs are named "Booker <case> <trigger>" (`run-name`), which is
how a run is matched to its case. After a real booking, booker.yml uploads an empty
`booking-booked-<case>` artifact (`booking-booked` for the default case); while it exists the
monitor starts no more bookers for that case. It is checked on every check (no local cache),
so deleting the artifact on GitHub re-enables booking at once.
Errors never include the token: they carry only the HTTP status and GitHub's message.
"""
import re
import time

import requests

from .cases import DEFAULT_ID, booked_artifact

API = "https://api.github.com/repos/{repo}/actions/{path}"
WORKFLOW = "booker.yml"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
# A dispatched run takes a few seconds to show up in the API; count it as running meanwhile.
DISPATCH_GRACE_SECONDS = 90
# One list of runs serves every case in the same check (checks are >= 20 s apart).
RUNS_CACHE_SECONDS = 10
RUN_NAME_RE = re.compile(r"^Booker (\S+) ")


class DispatchError(Exception):
    pass


def case_of(run):
    """Case id of a booker run from its title; runs from before cases are the default case."""
    m = RUN_NAME_RE.match(f"{run.get('display_title') or ''} ")
    return m.group(1) if m else DEFAULT_ID


class GitHubBooker:
    def __init__(self, repo, token, ref="main", session=None, monotonic=time.monotonic,
                 capacity=4):
        self.repo = repo
        self.token = token
        self.ref = ref
        self.s = session or requests.Session()
        self.monotonic = monotonic
        self.capacity = capacity
        self.dispatched = {}   # case id -> monotonic time of its last dispatch
        self.last_run = {}     # case id -> the active run seen by the latest running() call
        self._runs = None
        self._runs_at = None

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

    def booked(self, case):
        """True while this case's booked artifact (a real booking) exists."""
        body = self._call("GET", "artifacts",
                          params={"name": booked_artifact(case.id), "per_page": 1})
        return bool(body.get("total_count"))

    def _active_runs(self):
        now = self.monotonic()
        if self._runs is None or now - self._runs_at >= RUNS_CACHE_SECONDS:
            body = self._call("GET", f"workflows/{WORKFLOW}/runs", params={"per_page": 30})
            self._runs = [run for run in body.get("workflow_runs", [])
                          if run.get("status") in ACTIVE]
            self._runs_at = now
        return self._runs

    def _in_grace(self, case_id):
        at = self.dispatched.get(case_id)
        return at is not None and self.monotonic() - at < DISPATCH_GRACE_SECONDS

    def running(self, case):
        if self._in_grace(case.id):
            self.last_run[case.id] = None
            return True
        run = next((r for r in self._active_runs() if case_of(r) == case.id), None)
        self.last_run[case.id] = run
        return run is not None

    def active(self):
        """Booker runs going now, counting dispatches not visible on GitHub yet."""
        runs = self._active_runs()
        seen = {case_of(r) for r in runs}
        return len(runs) + sum(1 for c in self.dispatched if self._in_grace(c) and c not in seen)

    def describe(self, case):
        """Short, stable text about the case's active run (logged only when it changes)."""
        run = self.last_run.get(case.id)
        if run is None:
            return "dispatched, not visible on GitHub yet"
        return f"GitHub run {run.get('id')} {run.get('status')}"

    def start(self, case, trigger):
        """Dispatch booker.yml for this case unless its run is active. Raises DispatchError."""
        if self.running(case):
            return False
        self._call("POST", f"workflows/{WORKFLOW}/dispatches",
                   json={"ref": self.ref, "inputs": {"trigger": trigger, "case": case.id,
                                                     "city": case.city}})
        self.dispatched[case.id] = self.monotonic()
        return True

    def wait(self):
        pass  # the runs live on GitHub; nothing to wait for here
