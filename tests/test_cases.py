import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dian_booker.profile import Profile, ProfileError  # noqa: E402
from dian_booker.wizard import choose_city, choose_service  # noqa: E402
from dian_monitor import cases, events, state  # noqa: E402
from dian_monitor.airtable import Airtable, AirtableError, CasesFeed, last_slot  # noqa: E402
from dian_monitor.cases import Case, CasesError  # noqa: E402
from dian_monitor.check import FOUND, evaluate  # noqa: E402
from dian_monitor.config import Settings  # noqa: E402
from test_booker import make  # noqa: E402
from test_monitor import FRIDAY, Clock, FakeResponse  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
MED = "“Medellín - Solicitud de devolución y/o compensación Vehículos eléctricos”"
CALI = "“Cali - Solicitud de devolución y/o compensación Vehículos eléctricos”"
BOG = "“Bogotá D.C. - Solicitud de devolución y/o compensación Vehículos eléctricos”"
CALI_GENERAL = "“Cali - Solicitud de devolución y/o compensación persona natural”"
PASTO = "“Pasto - Solicitud de devolución Vehículos”"
NONE = {"Encontrado": False, "DetalleAdicional": "manejadorNoEncontroColas",
        "ErrorString": "No hay agendas disponibles"}
LEAKS = ("1017234567", "Ana", "ana@example.com", "3001234567")
TOKEN = "patSECRETTOKEN.abc"


def colas_with(*names):
    return {"Encontrado": True, "DetalleAdicional": "manejadorEncontroColas",
            "ObjetosEncontrados": [[{"Nombre": n, "Requisitos": "Bogotá, Cali y Medellín"}
                                    for n in names]]}


def person(cid, city, **extra):
    return {"id": cid, "city": city, "full_name": "Ana Pérez", "document_number": "1017234567",
            "email": "ana@example.com", "phone": "3001234567", **extra}


CASES = [Case("case-1", "medellin"), Case("case-2", "cali"), Case("case-3", "cali"),
         Case("case-4", "bogota")]


class FakeAirtable:
    """Answers list/get/update like the Airtable REST API, over in-memory records."""

    def __init__(self, records=(), fail=None):
        self.records = [dict(r) for r in records]
        self.fail = fail  # HTTP status to answer every call with
        self.calls = []

    def request(self, method, url, timeout, headers, params=None, json=None):
        self.calls.append((method, url, params, json))
        if self.fail:
            return FakeResponse(status=self.fail, body={"error": {"type": "AUTHENTICATION_REQUIRED",
                                                                   "message": "Bad token"}})
        if method == "PATCH":
            rec = next(r for r in self.records if url.endswith("/" + r["id"]))
            rec["fields"].update(json["fields"])
            return FakeResponse(body=rec)
        records = self.records
        formula = (params or {}).get("filterByFormula")
        if formula:
            number = int(re.match(r"\{ID\}=(\d+)$", formula).group(1))
            records = [r for r in records if r["fields"].get("ID") == number]
        wanted = (params or {}).get("fields[]")
        out = [{"id": r["id"], "fields": {k: v for k, v in r["fields"].items()
                                          if not wanted or k in wanted}} for r in records]
        return FakeResponse(body={"records": out})


def record(n, city, status=None, **extra):
    fields = {"ID": n, "City": city, "Full name": "Ana Pérez", "Document number": "1017234567",
              "Email": "ana@example.com", "Phone": "+57 300 123 4567", **extra}
    if status:
        fields["Status"] = status
    return {"id": f"rec{n:014d}", "fields": fields}


def airtable(fake):
    return Airtable(TOKEN, "appXXXXXXXXXXXXXX", "Cases", session=fake)


class ParseTest(unittest.TestCase):
    def test_example_file_is_valid(self):
        with open(os.path.join(ROOT, "cases.example.json"), encoding="utf-8") as f:
            parsed = cases.parse(f.read(), personal=True)
        self.assertEqual(sorted({c.city for c in parsed}),
                         ["bogota", "bucaramanga", "cali", "medellin"])
        self.assertTrue(all(c.status == "paused" for c in parsed))  # fake people: never booked
        for case in parsed:
            Profile.from_case(case)

    def test_monitor_never_keeps_personal_data(self):
        text = json.dumps({"cases": [person("case-1", "Medellín")]})
        routing = cases.parse(text)[0]
        self.assertEqual((routing.id, routing.city, routing.status),
                         ("case-1", "medellin", "pending"))
        self.assertEqual(routing.personal, {})
        full = cases.parse(text, personal=True)[0]
        self.assertEqual(full.personal["document_number"], "1017234567")
        self.assertNotIn("1017234567", repr(full))

    def test_rejects_bad_files_without_echoing_personal_data(self):
        bad = [
            "not json 1017234567",
            json.dumps({"cases": []}),
            json.dumps({"cases": [person("1017234567", "cali")]}),   # id is a document number
            json.dumps({"cases": [person("Ana Pérez", "cali")]}),
            json.dumps({"cases": [person("c1", "cali"), person("c1", "cali")]}),
            json.dumps({"cases": [person("c1", "pasto")]}),           # no appointments there
            json.dumps({"cases": [person("c1", "cali", ciudad="cali")]}),  # typo'd field
            json.dumps({"cases": [person("c1", "cali", status="maybe")]}),
            json.dumps({"cases": [person("default", "cali")]}),
        ]
        for text in bad:
            with self.assertRaises(CasesError, msg=text) as ctx:
                cases.parse(text)
            for leak in LEAKS:
                self.assertNotIn(leak, str(ctx.exception))

    def test_status_decides_what_is_watched(self):
        text = json.dumps({"cases": [person("c1", "cali"), person("c2", "cali", status="Paused"),
                                     person("c3", "cali", status="booked")]})
        self.assertEqual([c.enabled for c in cases.parse(text)], [True, False, False])

    def test_default_case_and_per_case_files(self):
        self.assertIsNone(cases.from_json_settings(Settings()))
        self.assertEqual(cases.default_case(Settings(city="Medellín")), Case("default", "medellin"))
        self.assertEqual(cases.booking_file(Settings(), "default"), "state/booking.json")
        self.assertEqual(cases.booking_file(Settings(), "case-1"), "state/booking-case-1.json")
        self.assertEqual(cases.booked_artifact("default"), "booking-booked")
        self.assertEqual(cases.booked_artifact("case-1"), "booking-booked-case-1")

    def test_profile_is_cedula_with_clean_digits(self):
        case = cases.parse(json.dumps({"cases": [person("c1", "cali", phone="+57 300 123 4567",
                                                        document_number="1.017.234.567")]}),
                           personal=True)[0]
        p = Profile.from_case(case)
        self.assertEqual((p.doc_number, p.phone, p.doc_type),
                         ("1017234567", "3001234567", "Cédula de ciudadanía"))

    def test_profile_errors_name_the_field_not_the_value(self):
        for extra, message in (({"email": "no-at-sign"}, "email is not an email"),
                               ({"phone": "300 12"}, "phone must have 10 digits"),
                               ({"document_number": "AB123"}, "document_number must be digits")):
            case = cases.parse(json.dumps({"cases": [person("c1", "cali", **extra)]}),
                               personal=True)[0]
            with self.assertRaises(ProfileError) as ctx:
                Profile.from_case(case)
            self.assertIn(f"case 'c1': {message}", str(ctx.exception))
            for value in extra.values():
                self.assertNotIn(value, str(ctx.exception))


class AirtableTest(unittest.TestCase):
    def test_routing_read_never_asks_for_personal_fields(self):
        fake = FakeAirtable([record(1, "Medellín"), record(2, "Cali", "Paused"),
                             record(3, "Cali", "Booked")])
        got, skipped = airtable(fake).routing_cases()
        self.assertEqual([(c.id, c.city, c.status) for c in got],
                         [("case-1", "medellin", "pending"), ("case-2", "cali", "paused"),
                          ("case-3", "cali", "booked")])
        self.assertEqual(skipped, [])
        self.assertEqual(fake.calls[0][2]["fields[]"], ["ID", "City", "Status"])
        self.assertTrue(all(c.personal == {} for c in got))

    def test_invalid_records_are_skipped_not_fatal(self):
        fake = FakeAirtable([record(1, "Medellín"), record(2, "Pasto"),
                             {"id": "recX", "fields": {"City": "Cali"}}])
        got, skipped = airtable(fake).routing_cases()
        self.assertEqual([c.id for c in got], ["case-1"])
        self.assertEqual(len(skipped), 2)
        self.assertIn("case-2", skipped[0])

    def test_case_reads_one_record_with_its_person(self):
        fake = FakeAirtable([record(1, "Medellín"), record(7, "Cali")])
        case = airtable(fake).case("case-7")
        self.assertEqual((case.id, case.city, case.record_id), ("case-7", "cali", "rec00000000000007"))
        self.assertEqual(Profile.from_case(case).phone, "3001234567")
        self.assertEqual(fake.calls[0][2]["filterByFormula"], "{ID}=7")
        with self.assertRaises(CasesError):
            airtable(fake).case("case-99")
        with self.assertRaises(CasesError):
            airtable(fake).case("med-1")

    def test_mark_booked(self):
        fake = FakeAirtable([record(7, "Cali")])
        airtable(fake).mark_booked("rec00000000000007", "martes 18 nov 8:00 AM",
                                   "2026-11-18T08:01:00-05:00")
        fields = fake.records[0]["fields"]
        self.assertEqual((fields["Status"], fields["Appointment"]),
                         ("Booked", "martes 18 nov 8:00 AM"))

    def test_errors_never_carry_the_token(self):
        with self.assertRaises(AirtableError) as ctx:
            airtable(FakeAirtable(fail=401)).routing_cases()
        self.assertIn("HTTP 401 AUTHENTICATION_REQUIRED: Bad token", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(Settings(airtable_token=TOKEN)))


class FeedTest(unittest.TestCase):
    def feed(self, fake, clock, cache=None, on_start=True):
        cache = cache or os.path.join(tempfile.mkdtemp(), "cases.json")
        return CasesFeed(airtable(fake), (6, 11, 14), cache, clock, lambda m: None,
                         refresh_on_start=on_start), cache

    def test_last_slot(self):
        at = lambda h, m=0, d=0: FRIDAY.replace(hour=h, minute=m) + timedelta(days=d)  # noqa: E731
        self.assertEqual(last_slot(at(9, 30), (6, 11, 14)), at(6))
        self.assertEqual(last_slot(at(14), (6, 11, 14)), at(14))
        self.assertEqual(last_slot(at(5, 59), (6, 11, 14)), at(14, d=-1))

    def test_reads_at_start_then_after_each_refresh_hour(self):
        fake = FakeAirtable([record(1, "Medellín")])
        clock = Clock(FRIDAY.replace(hour=9, minute=30))
        feed, _ = self.feed(fake, clock)
        self.assertEqual(len(feed.start()[0]), 1)
        reads = lambda: sum(1 for c in fake.calls if c[0] == "GET")  # noqa: E731
        clock.now = clock.now.replace(hour=10, minute=59)
        self.assertFalse(feed.due(clock.now))
        clock.now = clock.now.replace(hour=11, minute=0, second=20)
        self.assertTrue(feed.due(clock.now))
        feed.refresh()
        self.assertFalse(feed.due(clock.now.replace(hour=13, minute=59)))
        self.assertTrue(feed.due(clock.now.replace(hour=14, minute=1)))
        self.assertTrue(feed.due(clock.now.replace(hour=6, minute=5) + timedelta(days=1)))
        self.assertEqual(reads(), 2)

    def test_failure_uses_the_cache_and_retries_later(self):
        fake = FakeAirtable([record(1, "Medellín"), record(2, "Cali")])
        clock = Clock(FRIDAY)
        feed, cache = self.feed(fake, clock)
        feed.start()
        with open(cache, encoding="utf-8") as f:
            saved = f.read()
        for leak in LEAKS:
            self.assertNotIn(leak, saved)  # routing only on disk
        # A restart while Airtable is down: the cached cases are watched.
        fake.fail = 503
        restarted, _ = self.feed(fake, clock, cache)
        got, problem = restarted.start()
        self.assertEqual([c.id for c in got], ["case-1", "case-2"])
        self.assertIn("HTTP 503", problem)
        self.assertFalse(restarted.due(clock.now + timedelta(minutes=9)))
        self.assertTrue(restarted.due(clock.now + timedelta(minutes=10)))  # backoff, not every check
        fake.fail = None
        got, problem = restarted.refresh()
        self.assertIsNone(problem)

    def test_fallback_monitor_skips_the_start_read_when_the_cache_is_fresh(self):
        fake = FakeAirtable([record(1, "Medellín")])
        clock = Clock(FRIDAY.replace(hour=9))
        feed, cache = self.feed(fake, clock)
        feed.start()
        calls = len(fake.calls)
        clock.now += timedelta(minutes=20)
        again, _ = self.feed(fake, clock, cache, on_start=False)
        self.assertEqual(len(again.start()[0]), 1)
        self.assertEqual(len(fake.calls), calls)  # no call
        clock.now = clock.now.replace(hour=11, minute=5)
        stale, _ = self.feed(fake, clock, cache, on_start=False)
        stale.start()
        self.assertEqual(len(fake.calls), calls + 1)


class EvaluateCitiesTest(unittest.TestCase):
    def test_each_city_gets_its_own_tramites(self):
        result = evaluate(colas_with(MED, CALI, BOG, PASTO), ["cali", "medellin", "bogota"])
        self.assertEqual(result.status, FOUND)
        self.assertEqual(result.by_city, {"medellin": [MED], "cali": [CALI], "bogota": [BOG]})
        self.assertEqual(result.other_cities, [PASTO])

    def test_only_the_service_counts(self):
        result = evaluate(colas_with(CALI_GENERAL), ["cali"], "vehiculos")
        self.assertEqual((result.status, result.other_cities), ("NONE", [CALI_GENERAL]))
        result = evaluate(colas_with(CALI_GENERAL, CALI), ["cali"], "vehiculos")
        self.assertEqual(result.by_city, {"cali": [CALI]})

    def test_requisitos_never_count_and_unwatched_cities_are_not_found(self):
        result = evaluate(colas_with(CALI), ["medellin", "bogota"])
        self.assertEqual((result.status, result.by_city), ("NONE", {}))

    def test_city_must_be_the_prefix_itself(self):
        result = evaluate(colas_with("“Medellín Norte - X”", "“Bogotá D.C. - X”"),
                          ["medellin", "bogota"])
        self.assertEqual(list(result.by_city), ["bogota"])

    def test_wizard_city_option(self):
        self.assertEqual(choose_city(["", "Cali", "Bogotá D.C."], "bogota"), 2)
        self.assertEqual(choose_city(["", "Medellín Norte", "Medellín"], "medellin"), 2)
        self.assertIsNone(choose_city(["", "Medellín Norte"], "medellin"))

    def test_wizard_never_falls_back_to_another_tramite(self):
        options = ["", "Cali “Solicitud de devolución y/o compensación persona natural”."]
        self.assertIsNone(choose_service(options, "cali", "vehiculos", strict=True))
        self.assertEqual(choose_service(options, "cali", "vehiculos"), 1)  # old, lenient


class MultiCityMonitorTest(unittest.TestCase):
    def alerts(self, mon):
        return [m.text for m in mon.notifier.sent if m.urgent]

    def test_one_alert_per_open_city_and_one_booker_per_case(self):
        mon, fake = make([colas_with(MED, CALI, PASTO)], cases=CASES)
        mon.poll()
        alerts = self.alerts(mon)
        self.assertEqual(len(alerts), 2)
        self.assertTrue(any("CALI" in a and "case-2, case-3" in a for a in alerts))
        self.assertTrue(any("MEDELLÍN" in a for a in alerts))
        self.assertFalse(any("Pasto" in a for a in alerts))
        found = sorted(c for c, t in fake.started_cases if t == "found")
        self.assertEqual(found, ["case-1", "case-2", "case-3"])
        self.assertIn(("case-4", "watch"), fake.started_cases)  # Bogotá not open: silent watch
        self.assertEqual(set(mon.state["openings"]), {"medellin", "cali"})

    def test_general_tramite_is_not_an_alert(self):
        mon, fake = make([colas_with(CALI_GENERAL)], cases=CASES)
        mon.poll()
        self.assertEqual(self.alerts(mon), [])
        self.assertEqual(mon.state["openings"], {})

    def test_cities_open_and_close_independently(self):
        mon, fake = make([colas_with(MED, CALI), colas_with(CALI), NONE], cases=CASES)
        mon.poll()
        mon.poll()
        self.assertEqual(set(mon.state["openings"]), {"cali"})
        mon.poll()
        kinds = [(e["kind"], e.get("city")) for e in events.read(mon.settings.events_file)
                 if e["kind"] in ("opening", "closed")]
        self.assertEqual(kinds, [("opening", "cali"), ("opening", "medellin"),
                                 ("closed", "medellin"), ("closed", "cali")])
        self.assertTrue(all(h["closed"] for h in mon.state["history"]))
        self.assertEqual(mon._marker(), "MONITOR_OK_NONE")

    def test_parallel_bookers_are_capped_found_first(self):
        mon, fake = make([colas_with(MED, CALI)], cases=CASES)
        fake.capacity = 2
        mon.poll()
        self.assertEqual(len(fake.started_cases), 2)
        self.assertTrue(all(t == "found" for _, t in fake.started_cases))
        self.assertTrue(any(d.startswith("waiting")
                            for d in mon.state["booker_decisions"].values()))

    def test_a_booked_case_does_not_stop_the_others(self):
        mon, fake = make([colas_with(CALI)], cases=CASES)
        fake.booked_cases = {"case-2"}
        mon.poll()
        self.assertIn(("case-3", "found"), fake.started_cases)
        self.assertNotIn(("case-2", "found"), fake.started_cases)
        self.assertIn("Casos: case-3", next(a for a in self.alerts(mon) if "CALI" in a))

    def test_city_with_every_case_booked_is_not_alerted(self):
        mon, fake = make([colas_with(CALI)] * 2, cases=CASES)
        fake.booked_cases = {"case-2", "case-3"}
        mon.poll()
        mon.poll()
        texts = [m.text for m in mon.notifier.sent]
        self.assertFalse(any("HAY CITA" in t for t in texts))
        notes = [t for t in texts if "ya hay una cita registrada" in t]
        self.assertEqual(len(notes), 1)
        self.assertIn("booking-booked-case-2", notes[0])

    def test_paused_and_booked_cases_are_not_watched(self):
        mon, fake = make([colas_with(CALI)], cases=[Case("case-1", "cali", status="paused"),
                                                     Case("case-2", "cali", status="booked"),
                                                     Case("case-3", "medellin")])
        mon.poll()
        self.assertEqual(self.alerts(mon), [])
        self.assertEqual(fake.started_cases, [("case-3", "watch")])

    def test_no_personal_data_in_events_logs_or_state(self):
        text = json.dumps({"cases": [person("case-1", "medellin")]})
        out = StringIO()
        with redirect_stdout(out):
            mon, _ = make([colas_with(MED)], cases_json=text)
            mon.poll()
        logged = out.getvalue() + open(mon.settings.events_file, encoding="utf-8").read() \
            + open(mon.settings.state_file, encoding="utf-8").read()
        for leak in LEAKS:
            self.assertNotIn(leak, logged)


class MonitorFeedTest(unittest.TestCase):
    def make(self, fake, items, start=FRIDAY):
        tmp = tempfile.mkdtemp()
        clock = Clock(start)
        feed = CasesFeed(airtable(fake), (6, 11, 14), os.path.join(tmp, "cases.json"), clock,
                         lambda m: None)
        mon, booker = make(items, feed=feed)
        mon.clock = clock
        return mon, booker, clock

    def test_monitor_reads_airtable_at_start_and_follows_changes(self):
        fake = FakeAirtable([record(1, "Medellín"), record(2, "Cali", "Paused")])
        mon, _, clock = self.make(fake, [colas_with(MED)] * 3,
                                  start=FRIDAY.replace(hour=10, minute=50))
        self.assertEqual(mon.cities, ["medellin"])
        mon.poll()
        self.assertIn("medellin", mon.state["openings"])
        # Medellín's case is booked and Cali's resumed in Airtable; read again at 11:00.
        fake.records[0]["fields"]["Status"] = "Booked"
        fake.records[1]["fields"]["Status"] = "Pending"
        clock.now = clock.now.replace(hour=11, minute=0, second=30)
        mon.poll()
        self.assertEqual(mon.cities, ["cali"])
        self.assertNotIn("medellin", mon.state["openings"])  # closed: nobody waits for it
        closed = [e for e in events.read(mon.settings.events_file) if e["kind"] == "closed"]
        self.assertEqual(closed[-1]["reason"], "unwatched")
        self.assertEqual([e["kind"] for e in events.read(mon.settings.events_file)].count(
            "cases_refresh"), 2)

    def test_airtable_down_alerts_once_and_keeps_monitoring(self):
        fake = FakeAirtable([record(1, "Medellín")], fail=500)
        mon, _, clock = self.make(fake, [NONE] * 3)
        self.assertEqual(mon.cases, [])
        self.assertTrue(mon.poll())
        problems = [m for m in mon.notifier.sent if "no pude leer los casos" in m.text]
        self.assertEqual(len(problems), 1)
        fake.fail = None
        clock.now += timedelta(minutes=11)
        mon.poll()
        self.assertEqual(mon.cities, ["medellin"])
        self.assertTrue(any("volví a leer" in m.text for m in mon.notifier.sent))

    def test_skipped_records_are_reported_once(self):
        fake = FakeAirtable([record(1, "Medellín"), record(2, "Pasto")])
        mon, _, _ = self.make(fake, [NONE])
        notes = [m.text for m in mon.notifier.sent if "datos incompletos" in m.text]
        self.assertEqual(len(notes), 1)
        self.assertIn("case-2", notes[0])
        for leak in LEAKS:
            self.assertNotIn(leak, notes[0])


class StateMigrationTest(unittest.TestCase):
    def test_old_medellin_state_becomes_an_opening(self):
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"open_since": "2026-11-10T09:00:00-05:00", "open_matches": [MED],
                       "alerts_for_opening": 2, "last_alert_at": "2026-11-10T09:05:00-05:00",
                       "booked_notice_for": None, "booker_decision": "running",
                       "last_other_cities": [CALI],
                       "history": [{"opened": "2026-11-10T09:00:00-05:00", "closed": None,
                                    "matches": [MED]}]}, f)
        st = state.load(path)
        self.assertEqual(st["openings"]["medellin"]["alerts"], 2)
        self.assertEqual(st["last_open"], [MED, CALI])
        self.assertEqual(st["history"][0]["city"], "medellin")
        for old in state.LEGACY_KEYS:
            self.assertNotIn(old, st)


class BookerCaseTest(unittest.TestCase):
    def run_main(self, args, booker_cls=None, **env):
        from dian_booker import __main__ as entry
        old_env, old_booker = dict(os.environ), entry.Booker
        os.environ.update({"DIAN_EVENTS_FILE": os.path.join(tempfile.mkdtemp(), "e.jsonl"),
                           **env})
        if booker_cls:
            entry.Booker = booker_cls
        try:
            with redirect_stdout(StringIO()) as out:
                code = entry.main(args)
            return code, out.getvalue()
        finally:
            entry.Booker = old_booker
            os.environ.clear()
            os.environ.update(old_env)

    def test_unknown_or_paused_case_is_refused_and_reported(self):
        text = json.dumps({"cases": [person("case-1", "cali", status="paused")]})
        for cid in ("nobody", "case-1"):
            code, out = self.run_main(["--trigger", "found", "--case", cid],
                                      DIAN_CASES_JSON=text, BOOKER_DRY_RUN="")
            self.assertEqual(code, 2)
            self.assertIn("no arrancó", out)  # a Telegram note, not only a log line
            self.assertNotIn("1017234567", out)

    def test_real_booking_needs_the_vehiculos_tramite(self):
        text = json.dumps({"cases": [person("case-1", "cali")]})
        code, _ = self.run_main(["--case", "case-1", "--service", "persona natural"],
                                DIAN_CASES_JSON=text, BOOKER_DRY_RUN="")
        self.assertEqual(code, 2)

    def test_airtable_case_reads_its_person_lazily_and_records_the_booking(self):
        fake = FakeAirtable([record(3, "Cali")])
        seen = {}

        class FakeBooker:
            def __init__(self, settings, profile, notifier, **kw):
                seen.update(settings=settings, profile=profile, **kw)

            def run(self):
                seen["calls_before_profile"] = len(fake.calls)
                seen["person"] = seen["load_profile"]()
                seen["on_booked"]({"fecha": "martes 18 nov", "hora": "8:00 AM"})
                return "BOOKED"

        import dian_monitor.airtable as at
        old = at.requests.Session
        at.requests.Session = lambda: fake
        try:
            code, out = self.run_main(["--trigger", "watch", "--case", "case-3", "--city", "cali"],
                                      FakeBooker, AIRTABLE_TOKEN=TOKEN,
                                      AIRTABLE_BASE_ID="appXXXXXXXXXXXXXX", BOOKER_DRY_RUN="",
                                      DIAN_BOOKING_FILE="booked/booking.json")
        finally:
            at.requests.Session = old
        self.assertEqual(code, 0)
        self.assertEqual(seen["calls_before_profile"], 0)  # no call until the person is needed
        self.assertIsNone(seen["profile"])
        self.assertEqual(seen["person"].doc_number, "1017234567")
        self.assertEqual(seen["settings"].city, "cali")
        self.assertEqual(seen["settings"].booking_file, "booked/booking-case-3.json")
        self.assertEqual(seen["service_hint"], "vehiculos")
        fields = fake.records[0]["fields"]
        self.assertEqual((fields["Status"], fields["Appointment"]), ("Booked", "martes 18 nov 8:00 AM"))
        self.assertEqual(len(fake.calls), 2)  # one read, one write
        self.assertNotIn(TOKEN, out)

    def test_airtable_case_that_changed_is_not_booked(self):
        fake = FakeAirtable([record(3, "Cali", "Paused")])
        from dian_booker.__main__ import resolve
        settings = Settings(airtable_token=TOKEN, airtable_base="appXXXXXXXXXXXXXX")
        import dian_monitor.airtable as at
        old = at.requests.Session
        at.requests.Session = lambda: fake
        try:
            source = resolve(settings, "case-3", "cali", events.EventLog(
                os.path.join(tempfile.mkdtemp(), "e.jsonl"), lambda: FRIDAY, "booker"))
            with self.assertRaises(CasesError):
                source.load_profile()  # paused since the dispatch: never typed into DIAN
        finally:
            at.requests.Session = old

    def test_record_is_per_case_and_city(self):
        from dian_booker import booker
        from dian_monitor.notify import ConsoleNotifier
        tmp = tempfile.mkdtemp()
        recorded = []
        settings = replace(Settings(), city="cali",
                           booking_file=os.path.join(tmp, "booking-case-1.json"),
                           events_file=os.path.join(tmp, "events.jsonl"))
        b = booker.Booker(settings, None, ConsoleNotifier(), case=Case("case-1", "cali"),
                          on_booked=recorded.append)
        self.assertFalse(b._record({"servicio": "Medellín “Solicitud…”."}))
        self.assertTrue(b._record({"servicio": "Cali “Solicitud…”."}))
        with open(settings.booking_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["case"], "case-1")
        self.assertEqual(len(recorded), 1)

    def test_a_failed_airtable_write_keeps_the_booking(self):
        from dian_booker import booker
        from dian_monitor.notify import ConsoleNotifier
        tmp = tempfile.mkdtemp()
        settings = replace(Settings(), city="cali",
                           booking_file=os.path.join(tmp, "booking-case-1.json"),
                           events_file=os.path.join(tmp, "events.jsonl"))

        def broken(chosen):
            raise AirtableError("Airtable update: HTTP 422")

        notifier = ConsoleNotifier()
        b = booker.Booker(settings, None, notifier, case=Case("case-1", "cali"), on_booked=broken)
        self.assertTrue(b._record({"servicio": "Cali “Solicitud…”."}))
        self.assertTrue(any("márcala" in m.text or "Status = Booked" in m.text
                            for m in notifier.sent))


class ReportByCityTest(unittest.TestCase):
    def test_openings_and_decisions_are_grouped_by_city(self):
        t = "2026-11-17T09:00:0{}-05:00"
        sample = [
            {"t": t.format(0), "kind": "opening", "city": "cali", "at": t.format(0),
             "matches": [CALI]},
            {"t": t.format(0), "kind": "opening", "city": "medellin", "at": t.format(0),
             "matches": [MED]},
            {"t": t.format(1), "kind": "booker_decision", "city": "cali", "case": "case-2",
             "opening": t.format(0), "decision": "started", "detail": ""},
            {"t": t.format(2), "kind": "closed", "city": "cali", "opened": t.format(0),
             "minutes": 2.0},
            {"t": t.format(3), "kind": "airtable_call", "op": "list", "ok": True},
            {"t": t.format(4), "kind": "airtable_call", "op": "get", "ok": False},
        ]
        text = events.report(sample)
        self.assertIn("cali  open 2 min", text)
        self.assertIn("booker case-2 started", text)
        self.assertIn("medellin  close not seen", text)
        self.assertEqual(text.count("NO BOOKER"), 1)
        self.assertIn("Airtable calls: 2 (list 1, get 1; 1 failed)", text)


if __name__ == "__main__":
    unittest.main()
