import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dian_booker.profile import Profile, ProfileError  # noqa: E402
from dian_booker.wizard import choose_city  # noqa: E402
from dian_monitor import cases, events, state  # noqa: E402
from dian_monitor.cases import Case, CasesError  # noqa: E402
from dian_monitor.check import FOUND, evaluate  # noqa: E402
from dian_monitor.config import Settings  # noqa: E402
from test_booker import make  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
MED = "“Medellín - Solicitud de devolución y/o compensación Vehículos eléctricos”"
CALI = "“Cali - Solicitud de devolución y/o compensación Vehículos eléctricos”"
BOG = "“Bogotá D.C. - Solicitud de devolución y/o compensación persona natural”"
PASTO = "“Pasto - Solicitud de devolución”"
NONE = {"Encontrado": False, "DetalleAdicional": "manejadorNoEncontroColas",
        "ErrorString": "No hay agendas disponibles"}


def colas_with(*names):
    return {"Encontrado": True, "DetalleAdicional": "manejadorEncontroColas",
            "ObjetosEncontrados": [[{"Nombre": n, "Requisitos": "Bogotá, Cali y Medellín"}
                                    for n in names]]}


def person(cid, city, **extra):
    return {"id": cid, "city": city, "doc_number": "1017234567", "full_name": "Ana Pérez",
            "email": "ana@example.com", "phone": "3001234567", **extra}


CASES = [Case("med-1", "medellin"), Case("cali-1", "cali"), Case("cali-2", "cali"),
         Case("bog-1", "bogota")]


class ParseTest(unittest.TestCase):
    def test_example_file_is_valid(self):
        with open(os.path.join(ROOT, "cases.example.json"), encoding="utf-8") as f:
            parsed = cases.parse(f.read())
        self.assertEqual(sorted({c.city for c in parsed}),
                         ["bogota", "bucaramanga", "cali", "medellin"])
        self.assertEqual(sum(c.city == "cali" for c in parsed), 2)

    def test_monitor_never_keeps_personal_data(self):
        text = json.dumps({"cases": [person("med-1", "Medellín")]})
        routing = cases.parse(text)[0]
        self.assertEqual((routing.id, routing.city), ("med-1", "medellin"))
        self.assertEqual(routing.personal, {})
        self.assertEqual(cases.parse(text, personal=True)[0].personal["doc_number"], "1017234567")
        self.assertNotIn("1017234567", repr(cases.parse(text, personal=True)[0]))

    def test_rejects_bad_files_without_echoing_personal_data(self):
        bad = [
            "not json 1017234567",
            json.dumps({"cases": []}),
            json.dumps({"cases": [person("1017234567", "cali")]}),   # id is a document number
            json.dumps({"cases": [person("Ana Pérez", "cali")]}),
            json.dumps({"cases": [person("c1", "cali"), person("c1", "cali")]}),
            json.dumps({"cases": [person("c1", "pasto")]}),           # no appointments there
            json.dumps({"cases": [person("c1", "cali", ciudad="cali")]}),  # typo'd field
            json.dumps({"cases": [person("c1", "cali", enabled="yes")]}),
            json.dumps({"cases": [person("default", "cali")]}),
        ]
        for text in bad:
            with self.assertRaises(CasesError, msg=text) as ctx:
                cases.parse(text)
            for leak in ("1017234567", "Ana", "ana@example.com", "3001234567"):
                self.assertNotIn(leak, str(ctx.exception))

    def test_without_cases_there_is_one_default_case(self):
        only = cases.load(Settings(city="Medellín"))
        self.assertEqual(only, [Case("default", "medellin")])
        self.assertEqual(cases.booking_file(Settings(), "default"), "state/booking.json")
        self.assertEqual(cases.booking_file(Settings(), "cali-1"), "state/booking-cali-1.json")
        self.assertEqual(cases.booked_artifact("default"), "booking-booked")
        self.assertEqual(cases.booked_artifact("cali-1"), "booking-booked-cali-1")

    def test_file_and_find(self):
        path = os.path.join(tempfile.mkdtemp(), "cases.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cases": [person("cali-1", "cali", service="vehiculos"),
                                 person("off-1", "cali", enabled=False)]}, f)
        settings = Settings(cases_file=path)
        case = cases.find(settings, "cali-1")
        self.assertEqual(case.service, "vehiculos")
        self.assertEqual(Profile.from_case(case).doc_number, "1017234567")
        for missing in ("nobody", "off-1", "default"):
            with self.assertRaises(CasesError):
                cases.find(settings, missing)
        with self.assertRaises(CasesError):
            cases.find(Settings(), "cali-1")  # a case id but no cases JSON

    def test_profile_errors_name_the_field_not_the_value(self):
        case = cases.parse(json.dumps({"cases": [person("c1", "cali", email="no-at-sign")]}),
                           personal=True)[0]
        with self.assertRaises(ProfileError) as ctx:
            Profile.from_case(case)
        self.assertIn("case 'c1': email is not an email", str(ctx.exception))
        self.assertNotIn("no-at-sign", str(ctx.exception))


class EvaluateCitiesTest(unittest.TestCase):
    def test_each_city_gets_its_own_tramites(self):
        result = evaluate(colas_with(MED, CALI, BOG, PASTO), ["cali", "medellin", "bogota"])
        self.assertEqual(result.status, FOUND)
        self.assertEqual(result.by_city, {"medellin": [MED], "cali": [CALI], "bogota": [BOG]})
        self.assertEqual(result.other_cities, [PASTO])

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


class MultiCityMonitorTest(unittest.TestCase):
    def alerts(self, mon):
        return [m.text for m in mon.notifier.sent if m.urgent]

    def test_one_alert_per_open_city_and_one_booker_per_case(self):
        mon, fake = make([colas_with(MED, CALI, PASTO)], cases=CASES)
        mon.poll()
        alerts = self.alerts(mon)
        self.assertEqual(len(alerts), 2)
        self.assertTrue(any("CALI" in a and "cali-1, cali-2" in a for a in alerts))
        self.assertTrue(any("MEDELLÍN" in a for a in alerts))
        self.assertFalse(any("Pasto" in a for a in alerts))
        found = sorted(c for c, t in fake.started_cases if t == "found")
        self.assertEqual(found, ["cali-1", "cali-2", "med-1"])
        # Bogotá is not open: its case watches, silently.
        self.assertIn(("bog-1", "watch"), fake.started_cases)
        self.assertEqual(set(mon.state["openings"]), {"medellin", "cali"})

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
        self.assertTrue(all(t == "found" for _, t in fake.started_cases))  # found before watch
        decisions = mon.state["booker_decisions"]
        self.assertTrue(any(d.startswith("waiting") for d in decisions.values()))

    def test_a_booked_case_does_not_stop_the_others(self):
        mon, fake = make([colas_with(CALI)], cases=CASES)
        fake.booked_cases = {"cali-1"}
        mon.poll()
        self.assertIn(("cali-2", "found"), fake.started_cases)
        self.assertNotIn(("cali-1", "found"), fake.started_cases)
        alert = next(a for a in self.alerts(mon) if "CALI" in a)
        self.assertIn("Casos: cali-2", alert)

    def test_city_with_every_case_booked_is_not_alerted(self):
        mon, fake = make([colas_with(CALI)] * 2, cases=CASES)
        fake.booked_cases = {"cali-1", "cali-2"}
        mon.poll()
        mon.poll()
        texts = [m.text for m in mon.notifier.sent]
        self.assertFalse(any("HAY CITA" in t for t in texts))
        notes = [t for t in texts if "ya hay una cita registrada" in t]
        self.assertEqual(len(notes), 1)  # once per opening
        self.assertIn("booking-booked-cali-1", notes[0])

    def test_disabled_cases_are_not_watched(self):
        mon, fake = make([colas_with(CALI)], cases=[Case("cali-1", "cali", enabled=False),
                                                     Case("med-1", "medellin")])
        mon.poll()
        self.assertEqual(self.alerts(mon), [])
        self.assertEqual(fake.started_cases, [("med-1", "watch")])

    def test_no_personal_data_in_events_or_logs(self):
        text = json.dumps({"cases": [person("med-1", "medellin")]})
        out = StringIO()
        with redirect_stdout(out):
            mon, _ = make([colas_with(MED)], cases_json=text)
            mon.poll()
        logged = out.getvalue() + open(mon.settings.events_file, encoding="utf-8").read() \
            + open(mon.settings.state_file, encoding="utf-8").read()
        for leak in ("1017234567", "Ana", "ana@example.com", "3001234567"):
            self.assertNotIn(leak, logged)


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

    def test_continues_an_old_opening_without_a_new_alert(self):
        mon, _ = make([colas_with(MED)])
        mon.state["openings"]["medellin"] = {"since": "2026-10-23T09:29:00-05:00",
                                             "matches": [MED], "alerts": 1,
                                             "last_alert_at": "2026-10-23T09:29:50-05:00",
                                             "booked_notice": False}
        mon.poll()
        self.assertFalse(any("HAY CITA" in m.text for m in mon.notifier.sent))


class BookerCaseTest(unittest.TestCase):
    def run_main(self, args, **env):
        from dian_booker.__main__ import main
        old = dict(os.environ)
        os.environ.update(env)
        try:
            with redirect_stdout(StringIO()) as out:
                code = main(args)
            return code, out.getvalue()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_unknown_or_disabled_case_is_refused_and_reported(self):
        text = json.dumps({"cases": [person("cali-1", "cali", enabled=False)]})
        for cid in ("nobody", "cali-1"):
            code, out = self.run_main(["--trigger", "found", "--case", cid],
                                      DIAN_CASES_JSON=text, BOOKER_DRY_RUN="")
            self.assertEqual(code, 2)
            self.assertIn("no arrancó", out)  # a Telegram note, not only a log line
            self.assertNotIn("1017234567", out)

    def test_case_sets_city_booking_file_and_service(self):
        from dian_booker import __main__ as entry
        seen = {}

        class FakeBooker:
            def __init__(self, settings, profile, notifier, **kw):
                seen.update(city=settings.city, file=settings.booking_file, **kw)

            def run(self):
                return "NOT_OPEN"

        text = json.dumps({"cases": [person("bog-1", "bogota", service="persona natural")]})
        old = entry.Booker
        entry.Booker = FakeBooker
        try:
            code, _ = self.run_main(["--trigger", "watch", "--case", "bog-1"],
                                    DIAN_CASES_JSON=text, BOOKER_SERVICE="vehiculos",
                                    DIAN_BOOKING_FILE="booked/booking.json", BOOKER_DRY_RUN="")
        finally:
            entry.Booker = old
        self.assertEqual(code, 0)
        self.assertEqual(seen["city"], "bogota")
        self.assertEqual(seen["file"], "booked/booking-bog-1.json")
        self.assertEqual(seen["service_hint"], "persona natural")  # the case wins
        self.assertEqual(seen["case"].id, "bog-1")

    def test_record_is_per_case_and_city(self):
        from dian_booker import booker
        from dian_monitor.notify import ConsoleNotifier
        tmp = tempfile.mkdtemp()
        case = Case("cali-1", "cali")
        settings = replace(Settings(), city="cali",
                           booking_file=os.path.join(tmp, "booking-cali-1.json"),
                           events_file=os.path.join(tmp, "events.jsonl"))
        b = booker.Booker(settings, Profile.from_env({
            "BOOKER_DOC_NUMBER": "1", "BOOKER_FULL_NAME": "Ana Pérez",
            "BOOKER_EMAIL": "a@b.co", "BOOKER_PHONE": "3001234567"}), ConsoleNotifier(), case=case)
        self.assertFalse(b._record({"servicio": "Medellín “Solicitud…”."}))
        self.assertTrue(b._record({"servicio": "Cali “Solicitud…”."}))
        with open(settings.booking_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["case"], "cali-1")


class ReportByCityTest(unittest.TestCase):
    def test_openings_and_decisions_are_grouped_by_city(self):
        t = "2026-11-17T09:00:0{}-05:00"
        sample = [
            {"t": t.format(0), "kind": "opening", "city": "cali", "at": t.format(0),
             "matches": [CALI]},
            {"t": t.format(0), "kind": "opening", "city": "medellin", "at": t.format(0),
             "matches": [MED]},
            {"t": t.format(1), "kind": "booker_decision", "city": "cali", "case": "cali-2",
             "opening": t.format(0), "decision": "started", "detail": ""},
            {"t": t.format(2), "kind": "closed", "city": "cali", "opened": t.format(0),
             "minutes": 2.0},
        ]
        text = events.report(sample)
        self.assertIn("cali  open 2 min", text)
        self.assertIn("booker cali-2 started", text)
        self.assertIn("medellin  close not seen", text)
        self.assertEqual(text.count("NO BOOKER"), 1)  # only Medellín's opening had none


if __name__ == "__main__":
    unittest.main()
