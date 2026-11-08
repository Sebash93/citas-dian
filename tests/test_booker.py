import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dian_booker import booker, remote  # noqa: E402
from dian_booker.profile import Profile, ProfileError  # noqa: E402
from dian_booker.remote import RemoteDisplay, novnc_url, tunnel_url_in  # noqa: E402
from dian_booker.wizard import (CityMismatch, PageUnresponsive, Wizard,  # noqa: E402
                                choose_option, choose_service, is_unresponsive, mentions_city)
from dian_monitor import events  # noqa: E402
from dian_monitor.config import Settings  # noqa: E402
from dian_monitor.monitor import BookerProcess, Monitor, already_booked  # noqa: E402
from dian_monitor.notify import ConsoleNotifier  # noqa: E402
from test_monitor import FRIDAY, Clock, FakeClient, colas  # noqa: E402

ENV = {"BOOKER_DOC_NUMBER": "1.017.234.567", "BOOKER_FULL_NAME": "Ana Pérez",
       "BOOKER_EMAIL": "ana@example.com", "BOOKER_PHONE": "+57 300 123 4567"}


class ChooseTest(unittest.TestCase):
    def test_videoatencion_picks_only_the_target_city(self):
        options = ["", "“Cali - Solicitud de devolución”", "“Medellín - Solicitud de devolución”"]
        self.assertEqual(choose_service(options, "medellin"), 2)
        self.assertIsNone(choose_service(options[:2], "medellin"))

    def test_wizard_format_without_dash_never_falls_back_to_another_city(self):
        # Booker run 41072506951 (2026-10-30) booked this first option: Bogotá.
        options = ["", 'Bogotá "Solicitud de devolución y/o compensación persona natural".',
                   "Bucaramanga “Solicitud de devolución y/o compensación persona natural”.",
                   "Cali “Solicitud de devolución y/o compensación persona natural”."]
        self.assertIsNone(choose_service(options, "medellin"))
        options.append("Medellín “Solicitud de devolución y/o compensación persona natural”.")
        self.assertEqual(choose_service(options, "medellin"), 4)

    def test_names_without_any_city_are_never_chosen(self):
        options = ["", "Inscripción o actualización RUT persona natural.", "Libros de contabilidad"]
        self.assertIsNone(choose_service(options, "medellin"))
        self.assertIsNone(choose_service(options, "medellin", "libros"))

    def test_preference_then_any_medellin_tramite_never_another_city(self):
        options = ["", "Cali “Solicitud de devolución y/o compensación Vehículos eléctricos”.",
                   "Medellín “Solicitud de devolución y/o compensación persona natural”.",
                   "“Medellín - Solicitud de devolución y/o compensación Vehículos eléctricos”"]
        self.assertEqual(choose_service(options, "medellin", "vehiculos"), 3)  # preferred
        without = options[:3]  # Medellín vehículos not offered: the general one, not Cali's
        self.assertEqual(choose_service(without, "medellin", "vehiculos"), 2)
        self.assertEqual(choose_service(options, "medellin", "no existe, vehiculos"), 3)
        self.assertIsNone(choose_service(options[:2], "medellin", "vehiculos"))

    def test_city_must_be_a_whole_word(self):
        self.assertTrue(mentions_city("“MEDELLÍN - x”", "medellin"))
        self.assertFalse(mentions_city("Medellinense x", "medellin"))

    def test_city_option_ignores_accents(self):
        self.assertEqual(choose_option(["", "Manizales", "Medellín"], "medellin"), 2)
        self.assertEqual(choose_option(["", "Cédula de ciudadanía"], "Cedula de Ciudadania"), 1)
        self.assertIsNone(choose_option(["", "Medellín Norte"], "medellin"))


class ProfileTest(unittest.TestCase):
    def test_reads_and_normalizes(self):
        p = Profile.from_env(ENV)
        self.assertEqual(p.doc_number, "1017234567")
        self.assertEqual(p.phone, "573001234567")
        self.assertEqual(p.doc_type, "Cédula de ciudadanía")

    def test_repr_hides_personal_data(self):
        text = repr(Profile.from_env(ENV)) + str(Profile.from_env(ENV))
        for secret in ("1017234567", "Ana", "ana@example.com", "3001234567"):
            self.assertNotIn(secret, text)

    def test_missing_fields(self):
        with self.assertRaises(ProfileError) as ctx:
            Profile.from_env({"BOOKER_DOC_NUMBER": "1"})
        self.assertIn("BOOKER_EMAIL", str(ctx.exception))


class RemoteTest(unittest.TestCase):
    def test_tunnel_url_from_cloudflared_log(self):
        line = "2026-10-26T22:00:00Z INF |  https://brave-lion-sun-x1.trycloudflare.com  |"
        self.assertEqual(tunnel_url_in(line), "https://brave-lion-sun-x1.trycloudflare.com")
        self.assertIsNone(tunnel_url_in("INF Starting tunnel"))

    def test_novnc_url_autoconnects_with_password(self):
        url = novnc_url("https://x.trycloudflare.com", "ab12cd34")
        self.assertTrue(url.startswith("https://x.trycloudflare.com/vnc.html?autoconnect=1"))
        self.assertIn("password=ab12cd34", url)

    def test_live_link_never_printed(self):
        notifier = ConsoleNotifier()
        secret = "https://x.trycloudflare.com/vnc.html?password=ab12cd34"
        from contextlib import redirect_stdout
        from io import StringIO
        out = StringIO()
        with redirect_stdout(out):
            notifier.send(booker.msg_get_ready(secret))
            notifier.send(booker.msg_tap_captcha(secret, {"hora": "8:00 AM"}))
            notifier.send(booker.msg_failed("datos", "x", secret, 10))
        self.assertNotIn("ab12cd34", out.getvalue())


class ExitCodeTest(unittest.TestCase):
    def test_only_a_broken_booker_fails_the_run(self):
        from dian_booker.__main__ import exit_code
        for result in ("BOOKED", "DRY_RUN_OK", "NOT_OPEN", "SLOT_GONE", "CAPTCHA_TIMEOUT"):
            self.assertEqual(exit_code(result), 0, result)
        self.assertEqual(exit_code("FAILED"), 1)


class SafetyStopTest(unittest.TestCase):
    def test_check_city_stops_a_non_medellin_tramite(self):
        w = Wizard(page=None, settings=Settings(), log=print)
        w.chosen["servicio"] = 'Bogotá "Solicitud de devolución y/o compensación persona natural".'
        with self.assertRaises(CityMismatch):
            w.check_city()
        w.chosen["servicio"] = "Medellín “Solicitud de devolución y/o compensación persona natural”."
        w.check_city()

    def test_record_ignores_other_city_bookings(self):
        tmp = tempfile.mkdtemp()
        settings = replace(Settings(), booking_file=os.path.join(tmp, "booking.json"),
                           events_file=os.path.join(tmp, "events.jsonl"))
        b = booker.Booker(settings, Profile.from_env(ENV), ConsoleNotifier())
        self.assertFalse(b._record({"servicio": 'Bogotá "Solicitud…".'}))
        self.assertFalse(already_booked(settings.booking_file))
        self.assertTrue(b._record({"servicio": "Medellín “Solicitud…”."}))
        self.assertTrue(already_booked(settings.booking_file))

    def test_timeouts_count_as_unresponsive(self):
        class TimeoutError(Exception):  # stands in for Playwright's, matched by name
            pass
        self.assertTrue(is_unresponsive(TimeoutError("Timeout 95000ms exceeded")))
        self.assertTrue(is_unresponsive(PageUnresponsive("x")))
        self.assertFalse(is_unresponsive(ValueError("x")))

    def test_slow_page_is_retried_not_failed(self):
        class TimeoutError(Exception):
            pass
        w = Wizard(page=None, settings=Settings(), log=lambda m: None)
        answers = [TimeoutError("Timeout 95000ms exceeded"), None, "Medellín “Solicitud…”."]
        w.find_service = lambda: (lambda a: (_ for _ in ()).throw(a)
                                  if isinstance(a, Exception) else a)(answers.pop(0))
        self.assertEqual(w.wait_for_service(float("inf"), retry_seconds=0), "Medellín “Solicitud…”.")
        w.find_service = lambda: (_ for _ in ()).throw(TimeoutError("again"))
        self.assertIsNone(w.wait_for_service(0, retry_seconds=0))
        self.assertTrue(w.unresponsive)  # → UNRESPONSIVE, not FAILED

    def test_real_booking_outside_medellin_is_refused(self):
        from dian_booker.__main__ import main
        env = {**ENV, "DIAN_CITY": "bogota", "BOOKER_DRY_RUN": ""}
        old = dict(os.environ)
        os.environ.update(env)
        try:
            self.assertEqual(main(["--trigger", "manual"]), 2)
        finally:
            os.environ.clear()
            os.environ.update(old)


class FakeDisplay:
    """A tunnel that stays down until the test lets it come up."""
    env = {}

    def __init__(self, error=None):
        self.up = threading.Event()
        self.error = error

    def url(self):
        self.up.wait(5)
        if self.error:
            raise self.error
        return "https://x.trycloudflare.com/vnc.html"


class LinkOrderTest(unittest.TestCase):
    def attempt(self, trigger, display, service="Medellín “Solicitud…”."):
        tmp = tempfile.mkdtemp()
        settings = replace(Settings(), booking_file=os.path.join(tmp, "booking.json"),
                           events_file=os.path.join(tmp, "events.jsonl"))
        notifier = ConsoleNotifier()
        b = booker.Booker(settings, Profile.from_env(ENV), notifier, trigger=trigger,
                          shots_dir=os.path.join(tmp, "shots"))
        seen = {}

        class FakeWizard:
            unresponsive = False
            chosen = {"servicio": service}

            def __init__(self, *args):
                pass

            def wait_for_service(self, deadline):
                seen["wizard_started_before_tunnel"] = not display.up.is_set()
                display.up.set()
                return service

            def pick_slot(self):
                pass

            def before_captcha(self, profile):
                pass

            def wait_captcha(self, deadline):
                return False

        with mock.patch.object(booker, "Wizard", FakeWizard):
            b.captcha_minutes = 0
            result = b._attempt(None, display, time.monotonic())
        return result, seen, [m.text for m in notifier.sent]

    def test_wizard_does_not_wait_for_the_tunnel_and_messages_stay_in_order(self):
        result, seen, texts = self.attempt("found", FakeDisplay())
        self.assertTrue(seen["wizard_started_before_tunnel"])
        self.assertEqual(result, booker.CAPTCHA_TIMEOUT)
        self.assertIn("PREPÁRATE", texts[0])
        self.assertIn("TOCA EL CAPTCHA", texts[1])

    def test_watch_sends_the_link_only_when_medellin_appears(self):
        display = FakeDisplay()
        display.up.set()
        tmp = tempfile.mkdtemp()
        settings = replace(Settings(), events_file=os.path.join(tmp, "events.jsonl"))
        notifier = ConsoleNotifier()
        b = booker.Booker(settings, Profile.from_env(ENV), notifier, trigger="watch")

        class NotOffered:
            unresponsive = False

            def __init__(self, *args):
                pass

            def wait_for_service(self, deadline):
                return None

        with mock.patch.object(booker, "Wizard", NotOffered):
            self.assertEqual(b._attempt(None, display, time.monotonic()), booker.NOT_OPEN)
        self.assertEqual(notifier.sent, [])

    def test_a_tunnel_failure_is_reported_at_the_captcha(self):
        display = FakeDisplay(error=RuntimeError("no tunnel"))
        result, _, texts = self.attempt("found", display)
        self.assertEqual(result, booker.FAILED)
        self.assertIn("no tunnel", texts[-1])


class RemoteDisplayStartTest(unittest.TestCase):
    def display(self):
        d = RemoteDisplay(412, 900, lambda m: None)
        self.addCleanup(d._closed.set)
        return d

    def test_browser_screen_does_not_wait_for_the_install(self):
        d = self.display()
        spawned = []
        d._spawn = lambda cmd, **kw: spawned.append(cmd[0])
        never_installed = os.path.join(tempfile.mkdtemp(), "not-ready")
        with mock.patch.dict(os.environ, {"BOOKER_READY_FILE": never_installed}), \
                mock.patch.object(remote.shutil, "which", return_value="/usr/bin/Xvfb"), \
                mock.patch.object(remote.os.path, "exists", side_effect=lambda p: p.endswith("X99")):
            d.__enter__()  # returns even though the packages are not installed
        self.assertEqual(spawned, ["Xvfb"])

    def test_url_reports_a_background_failure(self):
        d = self.display()
        d._start_remote_access = mock.Mock(side_effect=RuntimeError("no x11vnc"))
        d._expose()
        with self.assertRaisesRegex(RuntimeError, "no x11vnc"):
            d.url(timeout=1)


class FakeBooker:
    def __init__(self):
        self.started = []
        self.busy = False
        self.is_booked = False

    def booked(self):
        return self.is_booked

    def describe(self):
        return "fake run"

    def running(self):
        return self.busy

    def start(self, trigger):
        if self.busy:
            return False
        self.started.append(trigger)
        self.busy = True
        return True

    def wait(self):
        self.busy = False


def make(items, latencies=None, **overrides):
    tmp = tempfile.mkdtemp()
    settings = replace(Settings(), state_file=os.path.join(tmp, "state.json"),
                       captures_dir=os.path.join(tmp, "captures"),
                       booking_file=os.path.join(tmp, "booking.json"), heartbeat_hour=23,
                       events_file=os.path.join(tmp, "events.jsonl"),
                       **overrides)
    clock = Clock(FRIDAY)
    fake = FakeBooker()
    mon = Monitor(settings, ConsoleNotifier(), client=FakeClient(items, latencies), clock=clock,
                  sleep=clock.sleep, rand=lambda a, b: (a + b) / 2, booker=fake)
    return mon, fake


class BookerLaunchTest(unittest.TestCase):
    def test_opening_starts_one_booker_and_run_waits_for_it(self):
        mon, fake = make([colas("colas_medellin.json")] * 40)
        mon.run(budget_seconds=630)
        self.assertEqual(fake.started, ["found"])  # checks while it runs: no second one
        self.assertFalse(fake.busy)                 # run() waited for it

    def test_booker_is_replaced_on_the_next_check_while_still_open(self):
        mon, fake = make([colas("colas_medellin.json")] * 3)
        mon.poll()
        fake.busy = False  # the first booker ended (e.g. SLOT_GONE) but Medellín is still open
        mon.poll()         # no alert is due yet, the booker starts again anyway
        self.assertEqual(fake.started, ["found", "found"])

    def test_every_decision_change_is_logged(self):
        mon, fake = make([colas("colas_medellin.json")] * 4)
        mon.poll()
        mon.poll()
        mon.poll()
        decisions = [(e["decision"], e["detail"]) for e in events.read(mon.settings.events_file)
                     if e["kind"] == "booker_decision"]
        self.assertEqual(decisions, [("started", ""), ("running", "fake run")])  # once each

    def test_slow_dian_starts_no_booker(self):
        mon, fake = make([colas("colas_none.json")], latencies=[30])
        mon.poll()
        self.assertEqual(fake.started, [])

    def test_other_city_starts_a_silent_watch(self):
        mon, fake = make([colas("colas_cali_real.json")])
        mon.poll()
        self.assertEqual(fake.started, ["watch"])
        self.assertEqual(mon.notifier.sent, [])  # never an alert for another city

    def test_city_that_stays_open_starts_only_one_watch(self):
        cali = colas("colas_cali_real.json")
        mon, fake = make([cali, cali, colas("colas_none.json"), cali])
        mon.poll()
        fake.busy = False  # the watch ended, Cali is still open
        mon.poll()
        self.assertEqual(fake.started, ["watch"])
        mon.poll()         # closed
        mon.poll()         # open again: a new signal
        self.assertEqual(fake.started, ["watch", "watch"])

    def test_watch_can_be_turned_off(self):
        mon, fake = make([colas("colas_cali_real.json")], booker_on_other_city=False)
        mon.poll()
        self.assertEqual(fake.started, [])

    def test_watch_is_silent_after_a_booking(self):
        mon, fake = make([colas("colas_cali_real.json")])
        fake.is_booked = True
        mon.poll()
        self.assertEqual(fake.started, [])
        self.assertEqual(mon.notifier.sent, [])

    def test_opening_during_a_watch_is_logged_under_the_opening(self):
        mon, fake = make([colas("colas_cali_real.json"), colas("colas_cali_real.json"),
                          colas("colas_medellin.json")])
        mon.poll()
        mon.state["last_other_cities"] = []  # a second new tramite while the watch runs
        mon.poll()                           # decision: running
        mon.poll()                           # Medellín: same "running", under this opening
        opening = mon.state["open_since"]
        self.assertTrue(any(e["kind"] == "booker_decision" and e["opening"] == opening
                            and e["decision"] == "running"
                            for e in events.read(mon.settings.events_file)))
        self.assertEqual(fake.started, ["watch"])

    def test_no_booker_after_a_booking_and_it_says_so_once(self):
        mon, fake = make([colas("colas_medellin.json")] * 3)
        fake.is_booked = True
        mon.poll()
        self.assertEqual(fake.started, [])
        notes = [m for m in mon.notifier.sent if "ya hay una cita" in m.text]
        self.assertEqual(len(notes), 1)
        mon.state["last_alert_at"] = None  # force a re-alert for the same opening
        mon.poll()
        self.assertEqual(len([m for m in mon.notifier.sent if "ya hay una cita" in m.text]), 1)

    def test_local_booker_reads_the_booking_file(self):
        path = os.path.join(tempfile.mkdtemp(), "booking.json")
        proc = BookerProcess(path)
        self.assertFalse(proc.booked())
        with open(path, "w") as f:
            json.dump({"booked": True}, f)
        self.assertTrue(proc.booked())

    def test_disabled_by_default(self):
        tmp = tempfile.mkdtemp()
        settings = Settings(state_file=os.path.join(tmp, "s.json"),
                            events_file=os.path.join(tmp, "e.jsonl"))
        self.assertIsNone(Monitor(settings, ConsoleNotifier(), client=FakeClient([])).booker)


if __name__ == "__main__":
    unittest.main()
