"""python -m dian_booker [--case ID] [--city CITY] [--trigger found|watch|manual] [--dry-run] [--local]"""
import argparse
import os
from dataclasses import replace

from dian_monitor import cadence
from dian_monitor import cases as cases_mod
from dian_monitor.airtable import Airtable, AirtableError
from dian_monitor.cases import ALLOWED_CITIES, DEFAULT_ID, Case, CasesError
from dian_monitor.check import normalize
from dian_monitor.config import Settings
from dian_monitor.events import EventLog
from dian_monitor.notify import Message, build_notifier

from .booker import EXPECTED, Booker, log
from .profile import Profile, ProfileError

# Real bookings are only for Videoatención (2) / Devoluciones (13), tramite IVA vehículos.
# Other codes and services are for --dry-run wizard checks on categories that happen to be open.
TARGET_ATENCION = "2"
TARGET_CATEGORIA = "13"
TARGET_SERVICE = "vehiculos"


def _int_env(name, default):
    return int(os.environ.get(name, "").strip() or default)


class CaseSource:
    """The case to book and how to get its person and record the booking.

    With Airtable, the monitor passes the case's city (not personal) so the booker can start
    without a call; the person is read only when needed (`load_profile`), once."""

    def __init__(self, case, load_profile, on_booked=None):
        self.case = case
        self.load_profile = load_profile
        self.on_booked = on_booked


def resolve(settings, case_id, city, events):
    airtable = Airtable.from_settings(
        settings, on_call=lambda op, ok: events.emit("airtable_call", op=op, ok=ok))
    if airtable is not None:
        cid = cases_mod.check_id(case_id, "case")
        full = {}

        def fetch():
            if "case" not in full:
                found = airtable.case(cid)
                if case.city != found.city:
                    raise CasesError(f"case {cid!r} is now in {found.city}, not {case.city}")
                if not found.enabled:
                    raise CasesError(f"case {cid!r} is {found.status} in Airtable")
                full["case"] = found
            return full["case"]

        if city:
            case = Case(cid, cases_mod.check_city(city, cid))
        else:  # started by hand: one call now to learn the city
            case = airtable.case(cid)
            full["case"] = case
        if not case.enabled:
            raise CasesError(f"case {cid!r} is {case.status} in Airtable")

        def on_booked(chosen):
            appointment = " ".join(v for v in (chosen.get("fecha"), chosen.get("hora")) if v)
            airtable.mark_booked(fetch().record_id, appointment or "(ver correo)",
                                 cadence.now_bogota().isoformat(timespec="seconds"))

        return CaseSource(Case(case.id, case.city, record_id=case.record_id),
                          lambda: Profile.from_case(fetch()), on_booked)

    listed = cases_mod.from_json_settings(settings, personal=True)
    if listed is not None:
        case = next((c for c in listed if c.id == case_id), None)
        if case is None:
            raise CasesError(f"case {case_id!r} is not in the cases JSON")
        if not case.enabled:
            raise CasesError(f"case {case_id!r} is {case.status}")
        profile = Profile.from_case(case)  # local file: check the person at once
        return CaseSource(Case(case.id, case.city), lambda: profile)

    if case_id != DEFAULT_ID:
        raise CasesError(f"case {case_id!r} given but no cases source is configured")
    profile = Profile.from_env()
    return CaseSource(cases_mod.default_case(settings), lambda: profile)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dian_booker", description=__doc__)
    parser.add_argument("--trigger", choices=("found", "watch", "manual"), default="manual",
                        help="found: send the live link at once; otherwise when the city appears")
    parser.add_argument("--dry-run", action="store_true",
                        default=os.environ.get("BOOKER_DRY_RUN", "") in ("1", "true"),
                        help="go through the captcha and the form but never click Agendar")
    parser.add_argument("--local", action="store_true",
                        help="use this computer's screen instead of Xvfb + noVNC + tunnel")
    parser.add_argument("--service", default="",
                        help="tramite name part to book (default DIAN_SERVICE, i.e. vehiculos)")
    parser.add_argument("--case", default=os.environ.get("BOOKER_CASE", "").strip() or DEFAULT_ID,
                        help="case id, e.g. case-3 (default: the BOOKER_* person, DIAN_CITY)")
    parser.add_argument("--city", default=os.environ.get("BOOKER_CITY", "").strip(),
                        help="the case's city, passed by the monitor so no Airtable call is needed")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    service = args.service or settings.service
    events = EventLog(settings.events_file, cadence.now_bogota, "booker")
    try:
        source = resolve(settings, args.case, args.city, events)
    except (CasesError, ProfileError, AirtableError) as e:
        log(f"Not starting: {e}.")
        if args.trigger != "manual":  # a dispatch the monitor thought would book: say why not
            build_notifier().send(Message(f"⚠️ El bot que agenda no arrancó: {e}.", urgent=True))
        return 2
    case = source.case
    # From here on the booker works for this case only: its city, its booking record.
    settings = replace(settings, city=case.city, service=service,
                       booking_file=cases_mod.booking_file(settings, case.id))
    if not args.dry_run:
        if case.city not in ALLOWED_CITIES:
            log(f"Not starting: real bookings are only for {sorted(ALLOWED_CITIES)} "
                f"(city={case.city!r}). Other cities can only be tested with --dry-run.")
            return 2
        if (settings.tipo_atencion, settings.categoria) != (TARGET_ATENCION, TARGET_CATEGORIA):
            log(f"Not starting: real bookings are only for Videoatención / Devoluciones "
                f"({settings.label}). Others can only be tested with --dry-run.")
            return 2
        if TARGET_SERVICE not in normalize(service):
            log(f"Not starting: real bookings are only for the {TARGET_SERVICE} tramite "
                f"(service={service!r}). Others can only be tested with --dry-run.")
            return 2
    log(f"Starting {case.id} ({args.trigger}{', dry run' if args.dry_run else ''}): "
        f"{settings.label} / {case.city_name} / {service}")
    result = Booker(settings, None, build_notifier(), trigger=args.trigger, case=case,
                    load_profile=source.load_profile, on_booked=source.on_booked,
                    dry_run=args.dry_run, local=args.local, service_hint=service,
                    wait_minutes=_int_env("BOOKER_WAIT_MINUTES", 12),
                    captcha_minutes=_int_env("BOOKER_CAPTCHA_MINUTES", 10),
                    hold_minutes=_int_env("BOOKER_HOLD_MINUTES", 10)).run()
    print(f"BOOKER_{result}", flush=True)
    return exit_code(result)


def exit_code(result):
    """0 for any outcome of the race for a slot (no slot is not an error), 1 when broken."""
    return 0 if result in EXPECTED else 1


if __name__ == "__main__":
    raise SystemExit(main())
