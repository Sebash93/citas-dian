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
from dian_booker.wizard import choose_option, choose_service  # noqa: E402
from dian_monitor.config import Settings  # noqa: E402
from dian_monitor.monitor import Monitor, already_booked  # noqa: E402
from dian_monitor.notify import ConsoleNotifier  # noqa: E402
from test_monitor import FRIDAY, Clock, FakeClient, colas  # noqa: E402

ENV = {"BOOKER_DOC_NUMBER": "1.017.234.567", "BOOKER_FULL_NAME": "Ana Pérez",
       "BOOKER_EMAIL": "ana@example.com", "BOOKER_PHONE": "+57 300 123 4567"}


class ChooseTest(unittest.TestCase):
    def test_videoatencion_picks_only_the_target_city(self):
        options = ["", "“Cali - Solicitud de devolución”", "“Medellín - Solicitud de devolución”"]
        self.assertEqual(choose_service(options, "medellin"), 2)
        self.assertIsNone(choose_service(options[:2], "medellin"))

    def test_names_without_city_use_the_hint_or_the_first(self):
        options = ["", "Inscripción o actualización RUT persona natural.", "Libros de contabilidad"]
        self.assertEqual(choose_service(options, "medellin", "libros"), 2)
        self.assertEqual(choose_service(options, "medellin"), 1)
        self.assertIsNone(choose_service(options, "medellin", "no existe"))

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


class FakeBooker:
    def __init__(self):
        self.started = []
        self.busy = False

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

    def test_no_booker_after_a_booking(self):
        mon, fake = make([colas("colas_medellin.json")])
        with open(mon.settings.booking_file, "w") as f:
            json.dump({"booked": True}, f)
        self.assertTrue(already_booked(mon.settings.booking_file))
        mon.poll()
        self.assertEqual(fake.started, [])

    def test_disabled_by_default(self):
        tmp = tempfile.mkdtemp()
        settings = Settings(state_file=os.path.join(tmp, "s.json"),
                            events_file=os.path.join(tmp, "e.jsonl"))
        self.assertIsNone(Monitor(settings, ConsoleNotifier(), client=FakeClient([])).booker)


if __name__ == "__main__":
    unittest.main()
