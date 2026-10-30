import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dian_booker.profile import Profile  # noqa: E402
from dian_monitor import events  # noqa: E402
from dian_monitor.client import SchemaChanged  # noqa: E402
from test_booker import ENV  # noqa: E402
from test_monitor import FRIDAY, Clock, colas, make_monitor  # noqa: E402


class EventLogTest(unittest.TestCase):
    def test_append_read_and_trim(self):
        path = os.path.join(tempfile.mkdtemp(), "sub", "events.jsonl")
        log = events.EventLog(path, Clock(FRIDAY), "monitor", run_id="42")
        for i in range(10):
            log.emit("x", i=i)
        got = events.read(path)
        self.assertEqual(len(got), 10)
        self.assertEqual(got[0], {"t": "2026-10-23T09:30:00-05:00", "src": "monitor",
                                  "kind": "x", "i": 0, "run": "42"})
        log.trim(max_lines=3)
        self.assertEqual([e["i"] for e in events.read(path)], [7, 8, 9])

    def test_never_raises(self):
        log = events.EventLog("/dev/null/cannot/write.jsonl", Clock(FRIDAY), "monitor")
        log.emit("x")
        log.trim()
        self.assertEqual(events.read("/no/such/file"), [])

    def test_skips_corrupt_lines(self):
        path = os.path.join(tempfile.mkdtemp(), "e.jsonl")
        with open(path, "w") as f:
            f.write('{"t": "2026-10-23T09:30:00-05:00", "kind": "x"}\n{"trunc\n')
        self.assertEqual(len(events.read(path)), 1)


class MonitorEventsTest(unittest.TestCase):
    def kinds(self, mon):
        return [e["kind"] for e in events.read(mon.settings.events_file)]

    def test_run_summary(self):
        none = colas("colas_none.json")
        mon, _, _ = make_monitor([none] * 10, heartbeat_hour=23)
        mon.run(budget_seconds=45 * 3)
        run = events.read(mon.settings.events_file)[-1]
        self.assertEqual(run["kind"], "run")
        self.assertEqual(run["marker"], "MONITOR_OK_NONE")
        self.assertEqual(run["checks"], 3)
        self.assertEqual(run["statuses"], {"NONE": 3})
        self.assertEqual(run["latency_max"], 1.0)

    def test_opening_closing_and_failures(self):
        found, none = colas("colas_medellin.json"), colas("colas_none.json")
        mon, _, clock = make_monitor([found, none, SchemaChanged("boom")], heartbeat_hour=23)
        mon.poll()
        clock.now += timedelta(minutes=3)
        mon.poll()
        mon.poll()
        got = events.read(mon.settings.events_file)
        self.assertEqual([e["kind"] for e in got],
                         ["opening", "booker_decision", "closed", "failure"])
        self.assertEqual((got[1]["decision"], got[1]["opening"]), ("off", got[0]["at"]))
        self.assertEqual(got[2]["minutes"], 3.0)
        self.assertEqual(got[2]["opened"], got[0]["at"])
        self.assertEqual(got[3]["error"], "SchemaChanged")

    def test_other_city_and_slow(self):
        mon, _, _ = make_monitor([colas("colas_cali_real.json")], latencies=[30],
                                 heartbeat_hour=23)
        mon.poll()
        self.assertEqual(self.kinds(mon), ["other_city", "slow", "booker_decision"])


class ScrubTest(unittest.TestCase):
    def test_masks_personal_data_in_any_form(self):
        p = Profile.from_env(ENV)
        text = ("Documento 1.017.234.567 de ANA PÉREZ (ana@example.com, 3001234567) "
                "en https://a-b.trycloudflare.com/vnc.html?password=x")
        clean = p.scrub(text)
        for leak in ("1.017.234.567", "ANA", "PÉREZ", "ana@example", "3001234567", "password"):
            self.assertNotIn(leak, clean)
        self.assertIn("Documento", clean)


class ReportTest(unittest.TestCase):
    def test_report_covers_monitor_and_booker(self):
        t = "2026-10-23T09:3{}:00-05:00"
        sample = [
            {"t": t.format(0), "kind": "run", "marker": "MONITOR_OK_NONE", "checks": 20,
             "errors": 1, "latency_max": 2.5},
            {"t": t.format(1), "kind": "failure", "error": "TimedOut"},
            {"t": t.format(2), "kind": "opening", "at": t.format(2), "matches": ["Medellín - X"]},
            {"t": t.format(3), "kind": "booker_stage", "stage": "captcha_shown", "seconds": 61.0},
            {"t": t.format(4), "kind": "dian_step", "step": "manejadorEncontroColas", "seconds": 8.2},
            {"t": t.format(5), "kind": "booker_end", "trigger": "found", "result": "FAILED",
             "stage": "datos", "seconds": 140.0, "error": "WizardError: x"},
            {"t": t.format(6), "kind": "closed", "opened": t.format(2), "minutes": 4.0},
            {"t": t.format(2), "kind": "booker_decision", "opening": t.format(2),
             "decision": "booked", "detail": "a Medellín booking is recorded"},
        ]
        text = events.report(sample)
        for expected in ("1 runs, 20 checks, 1 failed (5.0%)", "TimedOut 1", "open 4 min",
                         "NO BOOKER during this opening", "booker booked (a Medellín booking",
                         "FAILED at datos", "captcha_shown 61", "manejadorEncontroColas 8.2/8.2"):
            self.assertIn(expected, text)
        self.assertEqual(events.report([]), "No events.")


if __name__ == "__main__":
    unittest.main()
