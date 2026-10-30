"""Always-on hosting: --forever runs, health pings and the GitHub-dispatched booker."""
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dian_monitor import events  # noqa: E402
from dian_monitor.client import Blocked  # noqa: E402
from dian_monitor.config import Settings  # noqa: E402
from dian_monitor.health import Health  # noqa: E402
from dian_monitor.monitor import BookerProcess, Monitor, build_booker  # noqa: E402
from dian_monitor.notify import ConsoleNotifier  # noqa: E402
from dian_monitor.remote_booker import DispatchError, GitHubBooker  # noqa: E402
from test_monitor import FRIDAY, Clock, FakeClient, FakeResponse, colas  # noqa: E402

TOKEN = "github_pat_SECRET"


class FakeHealth:
    def __init__(self):
        self.pings = []

    def ping(self, status=""):
        self.pings.append(status)


class FakeGitHub:
    """Answers the three GitHub API calls the booker dispatcher makes."""

    def __init__(self, artifacts=0, runs=(), dispatch_status=204):
        self.artifacts = artifacts
        self.runs = list(runs)
        self.dispatch_status = dispatch_status
        self.calls = []

    def request(self, method, url, timeout, headers, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/artifacts"):
            return FakeResponse(body={"total_count": self.artifacts})
        if url.endswith("/runs"):
            return FakeResponse(body={"workflow_runs": [{"status": s} for s in self.runs]})
        if self.dispatch_status == 204:
            return FakeResponse(status=204)
        return FakeResponse(status=self.dispatch_status, body={"message": "Bad credentials"})


def settings_in_tmp(**overrides):
    tmp = tempfile.mkdtemp()
    return replace(Settings(), state_file=os.path.join(tmp, "state.json"),
                   captures_dir=os.path.join(tmp, "captures"),
                   booking_file=os.path.join(tmp, "booking.json"),
                   events_file=os.path.join(tmp, "events.jsonl"), heartbeat_hour=23, **overrides)


def make(items, booker=None, **overrides):
    clock = Clock(FRIDAY)
    health = FakeHealth()
    notifier = ConsoleNotifier()
    mon = Monitor(settings_in_tmp(**overrides), notifier, client=FakeClient(items), clock=clock,
                  sleep=clock.sleep, rand=lambda a, b: (a + b) / 2, booker=booker, health=health)
    return mon, health, notifier, clock


class ForeverTest(unittest.TestCase):
    def test_back_to_back_runs_keep_the_cadence(self):
        mon, health, _, clock = make([colas("colas_none.json")] * 200, run_budget_seconds=600)
        mon.run_forever(max_runs=3)
        runs = [e for e in events.read(mon.settings.events_file) if e["kind"] == "run"]
        self.assertEqual(len(runs), 3)
        # 45 s cadence on Friday morning: ~40 checks in 30 min, with no gap between runs.
        self.assertGreaterEqual(sum(r["checks"] for r in runs), 38)
        self.assertGreater(len(health.pings), 20)

    def test_sleeps_through_a_block_pause_and_keeps_pinging(self):
        mon, health, _, clock = make([Blocked("403"), colas("colas_none.json")],
                                     run_budget_seconds=60)
        mon.run_forever(max_runs=2)  # run 1 is blocked, run 2 is paused and waits it out
        self.assertGreaterEqual(clock.now, FRIDAY + timedelta(minutes=10))
        self.assertGreaterEqual(health.pings.count(""), 2)  # at most 5 min between pings

    def test_unexpected_error_is_retried(self):
        mon, health, _, clock = make([RuntimeError("bug")] + [colas("colas_none.json")] * 5,
                                     run_budget_seconds=60)
        mon.run_forever(max_runs=2)
        self.assertIn("fail", health.pings)
        got = events.read(mon.settings.events_file)
        self.assertEqual([e["kind"] for e in got if e["kind"] != "run"], ["crash"])
        runs = [e for e in got if e["kind"] == "run"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["errors"], 0)  # the crashed run's counts were not carried over

    def test_long_cadence_sleeps_are_sliced(self):
        mon, health, _, clock = make([], run_budget_seconds=60)
        mon._wait(15 * 60)
        self.assertEqual(health.pings, ["", "", ""])


class HealthTest(unittest.TestCase):
    def test_plain_pings_are_throttled_and_statuses_go_through(self):
        t = [0.0]
        session = FakeGitHub()
        sent = []
        session.get = lambda url, timeout: sent.append(url)
        health = Health("https://hc-ping.com/abc/", session=session, monotonic=lambda: t[0])
        health.ping()
        health.ping()
        t[0] = 61
        health.ping()
        health.ping("fail")
        self.assertEqual(sent, ["https://hc-ping.com/abc"] * 2 + ["https://hc-ping.com/abc/fail"])

    def test_no_url_means_no_requests(self):
        Health("", session=object()).ping()  # would fail on .get if it tried


class GitHubBookerTest(unittest.TestCase):
    def booker(self, gh):
        t = [0.0]
        return GitHubBooker("me/repo", TOKEN, session=gh, monotonic=lambda: t[0]), t

    def test_dispatches_with_trigger_and_counts_as_running_meanwhile(self):
        gh = FakeGitHub()
        b, t = self.booker(gh)
        self.assertTrue(b.start("found"))
        method, url, kwargs = gh.calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/repos/me/repo/actions/workflows/booker.yml/dispatches"))
        self.assertEqual(kwargs["json"], {"ref": "main", "inputs": {"trigger": "found"}})
        self.assertFalse(b.start("found"))  # not visible in the API yet, still "running"
        t[0] = 120
        self.assertTrue(b.start("found"))

    def test_active_run_blocks_a_second_dispatch(self):
        gh = FakeGitHub(runs=["completed", "in_progress"])
        b, _ = self.booker(gh)
        self.assertFalse(b.start("posible"))
        self.assertNotIn("POST", [c[0] for c in gh.calls])

    def test_booked_reads_the_artifact_every_time(self):
        gh = FakeGitHub(artifacts=1)
        b, _ = self.booker(gh)
        self.assertTrue(b.booked())
        gh.artifacts = 0  # artifact deleted on GitHub: booking is re-enabled at once
        self.assertFalse(b.booked())

    def test_booked_artifact_stops_the_monitor_from_dispatching(self):
        gh = FakeGitHub(artifacts=1)
        b, _ = self.booker(gh)
        mon, _, notifier, _ = make([colas("colas_medellin.json")], booker=b)
        mon.poll()
        self.assertNotIn("POST", [c[0] for c in gh.calls])
        self.assertTrue(any("ya hay una cita" in m.text for m in notifier.sent))

    def test_errors_never_carry_the_token(self):
        b, _ = self.booker(FakeGitHub(dispatch_status=401))
        with self.assertRaises(DispatchError) as ctx:
            b.start("found")
        self.assertIn("401 Bad credentials", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_missing_token(self):
        b = GitHubBooker("me/repo", "", session=FakeGitHub())
        with self.assertRaises(DispatchError):
            b.start("found")


class BookerChoiceTest(unittest.TestCase):
    def test_local_process_or_github(self):
        self.assertIsNone(build_booker(Settings()))
        self.assertIsInstance(build_booker(Settings(booker=True)), BookerProcess)
        self.assertIsInstance(build_booker(Settings(booker=True, booker_repo="me/repo")),
                              GitHubBooker)

    def test_dispatch_failure_alerts_and_keeps_monitoring(self):
        gh = GitHubBooker("me/repo", TOKEN, session=FakeGitHub(dispatch_status=401))
        mon, _, notifier, _ = make([colas("colas_medellin.json"), colas("colas_none.json")],
                                   booker=gh)
        self.assertTrue(mon.poll())
        self.assertTrue(mon.poll())
        texts = [m.text for m in notifier.sent]
        self.assertTrue(texts[0].startswith("🚨"))
        self.assertIn("No pude arrancar", texts[1])
        self.assertNotIn(TOKEN, json.dumps([e for e in events.read(mon.settings.events_file)]))


if __name__ == "__main__":
    unittest.main()
