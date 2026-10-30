import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dian_booker import booker  # noqa: E402
from dian_booker.profile import Profile, ProfileError  # noqa: E402
from dian_booker.remote import novnc_url, tunnel_url_in  # noqa: E402
from dian_booker.wizard import (CityMismatch, PageUnresponsive, Wizard,  # noqa: E402
                                choose_option, choose_service, is_unresponsive, mentions_city)
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

    def test_hint_chooses_among_medellin_tramites_only(self):
        options = ["", "“Medellín - Solicitud de devolución y/o compensación Vehículos eléctricos”",
                   "Medellín “Solicitud de devolución y/o compensación persona natural”.",
                   "Cali “Solicitud de devolución y/o compensación persona natural”."]
        self.assertEqual(choose_service(options, "medellin"), 1)
        self.assertEqual(choose_service(options, "medellin", "vehiculos"), 1)
        self.assertIsNone(choose_service(options[:1] + options[2:], "medellin", "vehiculos"))

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


class FakeBooker:
    def __init__(self):
        self.started = []
        self.busy = False
        self.is_booked = False

    def booked(self):
        return self.is_booked

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
        mon, fake = make([colas("colas_medellin.json")] * 20)
        mon.run(budget_seconds=45 * 14)
        self.assertEqual(fake.started, ["found"])  # re-alert while it runs: no second one
        self.assertFalse(fake.busy)                 # run() waited for it

    def test_posible_starts_booker(self):
        mon, fake = make([colas("colas_none.json")], latencies=[30])
        mon.poll()
        self.assertEqual(fake.started, ["posible"])

    def test_posible_can_be_turned_off(self):
        mon, fake = make([colas("colas_none.json")], latencies=[30], booker_on_possible=False)
        mon.poll()
        self.assertEqual(fake.started, [])

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
