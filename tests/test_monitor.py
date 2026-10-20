import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dian_monitor import cadence  # noqa: E402
from dian_monitor.check import FOUND, NONE, city_of, evaluate  # noqa: E402
from dian_monitor.client import (Blocked, DianClient, DianError, SchemaChanged,  # noqa: E402
                                 TimedOut)
from dian_monitor.config import Settings  # noqa: E402
from dian_monitor.monitor import Monitor  # noqa: E402
from dian_monitor.notify import ConsoleNotifier  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def colas(name):
    return json.loads(fixture(name))


# --- fakes -------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, text="", body=None, headers=None):
        self.status_code = status
        self.text = text
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeCookies:
    def clear(self):
        pass


class FakeSession:
    """Serves the page, the config, then queued ValidadorValidar responses."""

    def __init__(self, validar=()):
        self.headers = {}
        self.cookies = FakeCookies()
        self.validar = list(validar)
        self.gets = 0
        self.tokens_sent = []

    def get(self, url, timeout):
        self.gets += 1
        return FakeResponse(text=fixture("page.html"))

    def post(self, url, headers, data, timeout):
        self.tokens_sent.append(headers["RequestVerificationToken"])
        if url.endswith("ObtenerConfiguracion"):
            return FakeResponse(body={"d": fixture("config.json")})
        item = self.validar.pop(0)
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(body={"d": json.dumps(item)},
                            headers={"X-token": f"ROTATED-{len(self.tokens_sent)}"})


class FakeClient:
    """Monitor-level fake: each find_colas returns/raises the next queued item."""

    def __init__(self, items, latencies=None):
        self.items = list(items)
        self.latencies = list(latencies or [])
        self.requests_made = 0
        self.resets = 0
        self.last_latency = None

    def find_colas(self, *args):
        item = self.items.pop(0)
        self.last_latency = self.latencies.pop(0) if self.latencies else 1.0
        if isinstance(item, Exception):
            raise item
        return item

    def reset(self):
        self.resets += 1


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


# A Friday at 9:30 Bogota (high-frequency window).
FRIDAY = datetime(2026, 10, 23, 9, 30, tzinfo=cadence.BOGOTA)


def make_monitor(items, start=FRIDAY, latencies=None, **overrides):
    tmp = tempfile.mkdtemp()
    settings = replace(Settings(), state_file=os.path.join(tmp, "state.json"),
                       captures_dir=os.path.join(tmp, "captures"), **overrides)
    clock = Clock(start)
    notifier = ConsoleNotifier()
    mon = Monitor(settings, notifier, client=FakeClient(items, latencies), clock=clock,
                  sleep=clock.sleep, rand=lambda a, b: (a + b) / 2)
    return mon, notifier, clock


# --- check -------------------------------------------------------------------

class EvaluateTest(unittest.TestCase):
    def test_nothing_open(self):
        self.assertEqual(evaluate(colas("colas_none.json"), "medellin").status, NONE)

    def test_medellin_open(self):
        result = evaluate(colas("colas_medellin.json"), "medellin")
        self.assertEqual(result.status, FOUND)
        self.assertEqual(len(result.matches), 1)
        self.assertTrue(result.matches[0].startswith("“Medellín -"))
        self.assertEqual(len(result.other_cities), 1)

    def test_cali_whose_requisitos_mention_medellin_is_not_an_alert(self):
        # Real response (2026-10-20): Requisitos lists "Bogotá, Bucaramanga, Cali y Medellín".
        result = evaluate(colas("colas_cali_real.json"), "medellin")
        self.assertEqual(result.status, NONE)
        self.assertEqual(len(result.other_cities), 1)

    def test_city_prefix(self):
        self.assertEqual(city_of("“Cali - Solicitud de devolución…”"), "cali")
        self.assertEqual(city_of("Medell&iacute;n - Solicitud"), "medellin")
        self.assertEqual(city_of('"Bogotá D.C. - Solicitud"'), "bogota d.c.")
        self.assertIsNone(city_of("Solicitud de devolución sin ciudad"))
        # Another city whose name merely contains the target must not match.
        result = evaluate({"Encontrado": True, "DetalleAdicional": "manejadorEncontroColas",
                           "ObjetosEncontrados": [[{"Nombre": "“Medellín Norte - X”"}]]},
                          "medellin")
        self.assertEqual(result.status, NONE)

    def test_app_error_is_schema_change(self):
        with self.assertRaises(SchemaChanged):
            evaluate(colas("colas_app_error.json"), "medellin")

    def test_found_without_items_is_schema_change(self):
        with self.assertRaises(SchemaChanged):
            evaluate({"Encontrado": True, "DetalleAdicional": "manejadorEncontroColas",
                      "ObjetosEncontrados": [[]]}, "medellin")


# --- client ------------------------------------------------------------------

class ClientTest(unittest.TestCase):
    def test_token_rotates_through_x_token_header(self):
        session = FakeSession([colas("colas_none.json"), colas("colas_none.json")])
        client = DianClient(session=session)
        client.find_colas("1", "13", "2")
        client.find_colas("1", "13", "2")
        # config POST and first colas POST need a page token; the second colas uses X-token.
        self.assertEqual(session.gets, 2)
        self.assertEqual(session.tokens_sent,
                         ["TOKEN-FROM-PAGE", "TOKEN-FROM-PAGE", "ROTATED-2"])

    def test_config_is_fetched_once(self):
        session = FakeSession([colas("colas_none.json")] * 3)
        client = DianClient(session=session)
        for _ in range(3):
            client.find_colas("1", "13", "2")
        self.assertEqual(client.requests_made, 2 + 2 + 1 + 1)  # 2 GET + config + 3 colas

    def test_403_is_blocked(self):
        client = DianClient(session=FakeSession([FakeResponse(status=403)]))
        with self.assertRaises(Blocked):
            client.find_colas("1", "13", "2")

    def test_500_is_transient(self):
        client = DianClient(session=FakeSession([FakeResponse(status=502)]))
        with self.assertRaises(DianError) as ctx:
            client.find_colas("1", "13", "2")
        self.assertNotIsInstance(ctx.exception, (Blocked, SchemaChanged))

    def test_non_json_is_schema_change(self):
        client = DianClient(session=FakeSession([FakeResponse(text="<html>")]))
        with self.assertRaises(SchemaChanged):
            client.find_colas("1", "13", "2")


# --- monitor -----------------------------------------------------------------

class MonitorTest(unittest.TestCase):
    def urgent(self, notifier):
        return [m for m in notifier.sent if m.urgent]

    def test_opening_alerts_once_then_realerts_on_schedule(self):
        found = colas("colas_medellin.json")
        mon, notifier, clock = make_monitor([found] * 20, heartbeat_hour=23)
        mon.run(budget_seconds=45 * 14)  # polls every 45 s for 630 s
        alerts = self.urgent(notifier)
        # first alert at ~5 s, realerts once >= 300 s have passed: ~320 s and ~635 s (past budget)
        self.assertEqual(len(alerts), 2)
        self.assertIn("HAY CITA", alerts[0].text)
        self.assertIn("Sigue abierta", alerts[1].text)
        self.assertEqual(alerts[0].button[1], mon.settings.booking_url)
        self.assertTrue(os.listdir(mon.settings.captures_dir))

    def test_realerts_are_capped(self):
        found = colas("colas_medellin.json")
        mon, notifier, _ = make_monitor([found] * 60, heartbeat_hour=23)
        mon.run(budget_seconds=45 * 50)
        self.assertEqual(len(self.urgent(notifier)), 1 + mon.settings.max_realerts)

    def test_closing_records_history_and_new_opening_alerts_again(self):
        found, none = colas("colas_medellin.json"), colas("colas_none.json")
        mon, notifier, _ = make_monitor([found, none, found], heartbeat_hour=23)
        for _ in range(3):
            mon.poll()
        self.assertEqual(len(self.urgent(notifier)), 2)
        self.assertEqual(len(mon.state["history"]), 2)
        self.assertIsNotNone(mon.state["history"][0]["closed"])

    def test_other_city_is_silent(self):
        mon, notifier, _ = make_monitor([colas("colas_cali_real.json")], heartbeat_hour=23)
        mon.poll()
        self.assertEqual(notifier.sent, [])

    def test_failures_alert_once_after_threshold_then_recovery(self):
        err = SchemaChanged("boom")
        mon, notifier, _ = make_monitor([err, err, err, err, colas("colas_none.json")],
                                        heartbeat_hour=23)
        for _ in range(5):
            mon.poll()
        texts = [m.text for m in notifier.sent]
        self.assertEqual(len(texts), 2)
        self.assertIn("3 revisiones seguidas fallaron", texts[0])
        self.assertIn("volvieron a funcionar", texts[1])
        self.assertEqual(mon.client.resets, 4)

    def test_block_stops_run_and_pauses_next_run(self):
        none = colas("colas_none.json")
        mon, notifier, clock = make_monitor([Blocked("HTTP 429")] + [none] * 5,
                                            heartbeat_hour=23)
        self.assertEqual(mon.run(budget_seconds=600), "MONITOR_BLOCKED")
        self.assertEqual(len(mon.client.items), 5)  # stopped right after the block
        clock.now += timedelta(minutes=5)
        self.assertEqual(mon.run(budget_seconds=600), "MONITOR_PAUSED")
        clock.now += timedelta(minutes=10)
        self.assertEqual(mon.run(budget_seconds=60), "MONITOR_OK_NONE")

    def test_respects_cadence_across_runs(self):
        # Saturday night: 15-minute interval. A run 5 minutes after the last check must not poll.
        saturday = datetime(2026, 10, 24, 22, 0, tzinfo=cadence.BOGOTA)
        mon, _, clock = make_monitor([colas("colas_none.json")] * 3, start=saturday,
                                     heartbeat_hour=23)
        mon.poll()
        clock.now += timedelta(minutes=5)
        mon.run(budget_seconds=240)
        self.assertEqual(len(mon.client.items), 2)

    def test_heartbeat_once_per_day(self):
        mon, notifier, clock = make_monitor([colas("colas_none.json")] * 4)
        mon.run(once=True)
        mon.run(once=True)
        clock.now += timedelta(days=1)
        mon.run(once=True)
        beats = [m for m in notifier.sent if "sigue vivo" in m.text]
        self.assertEqual(len(beats), 2)
        self.assertIn("2 revisiones", beats[1].text)


class SlowDianTest(unittest.TestCase):
    def possibles(self, notifier):
        return [m for m in notifier.sent if "POSIBLE" in m.text]

    def test_slow_answer_sends_one_possible_alert_with_cooldown(self):
        none = colas("colas_none.json")
        mon, notifier, clock = make_monitor([none] * 4, latencies=[2, 25, 30, 22],
                                            heartbeat_hour=23)
        mon.poll()                      # 2 s: normal
        self.assertEqual(self.possibles(notifier), [])
        mon.poll()                      # 25 s: posible
        clock.now += timedelta(minutes=10)
        mon.poll()                      # still slow, inside the 30 min cooldown
        self.assertEqual(len(self.possibles(notifier)), 1)
        self.assertIn("25 s", self.possibles(notifier)[0].text)
        self.assertTrue(self.possibles(notifier)[0].urgent)
        clock.now += timedelta(minutes=25)
        mon.poll()                      # cooldown over
        self.assertEqual(len(self.possibles(notifier)), 2)
        self.assertEqual(len(mon.state["slow_events"]), 3)

    def test_timeout_is_a_possible_and_a_failure(self):
        mon, notifier, _ = make_monitor([TimedOut("no answer")], heartbeat_hour=23)
        mon.poll()
        self.assertEqual(len(self.possibles(notifier)), 1)
        self.assertIn("no respondió", notifier.sent[0].text)
        self.assertEqual(mon.state["consecutive_failures"], 1)

    def test_no_possible_when_medellin_is_confirmed(self):
        found = colas("colas_medellin.json")
        mon, notifier, _ = make_monitor([found, found], latencies=[30, 30], heartbeat_hour=23)
        mon.poll()
        mon.poll()
        self.assertEqual(self.possibles(notifier), [])
        self.assertEqual(len([m for m in notifier.sent if "HAY CITA" in m.text]), 1)


class TelegramTest(unittest.TestCase):
    def test_urgent_alert_has_sound_and_booking_button(self):
        from dian_monitor import notify

        class Session:
            calls = []

            def post(self, url, json, timeout):
                self.calls.append((url, json))
                return FakeResponse(body={"ok": True, "result": {}})

        session = Session()
        tg = notify.TelegramNotifier("TOKEN", ["111", "-222"], session=session)
        msg = notify.opening(Settings(), ["Medellín devoluciones"], FRIDAY)
        self.assertTrue(tg.send(msg))
        self.assertEqual([c[1]["chat_id"] for c in session.calls], ["111", "-222"])
        payload = session.calls[0][1]
        self.assertFalse(payload["disable_notification"])
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["url"], "https://agendamiento.dian.gov.co/?recurso=CitasDIAN")


class CadenceTest(unittest.TestCase):
    def test_windows(self):
        at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=cadence.BOGOTA)  # noqa: E731
        self.assertEqual(cadence.interval_for(at(23, 9, 30)), 45)   # Friday peak
        self.assertEqual(cadence.interval_for(at(20, 9)), 50)       # Tuesday morning
        self.assertEqual(cadence.interval_for(at(19, 15)), 75)      # Monday afternoon
        self.assertEqual(cadence.interval_for(at(19, 20)), 300)     # weekday evening
        self.assertEqual(cadence.interval_for(at(25, 11)), 900)     # Sunday


if __name__ == "__main__":
    unittest.main()
