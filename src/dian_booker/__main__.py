"""python -m dian_booker [--case ID] [--trigger found|watch|manual] [--dry-run] [--local]"""
import argparse
import os
from dataclasses import replace

from dian_monitor import cases as cases_mod
from dian_monitor.cases import ALLOWED_CITIES, DEFAULT_ID, CasesError
from dian_monitor.config import Settings
from dian_monitor.notify import Message, build_notifier

from .booker import EXPECTED, Booker, log
from .profile import Profile, ProfileError

# Real bookings are only for Videoatención (2) / Devoluciones (13). Other codes are for
# --dry-run wizard checks on categories that happen to be open.
TARGET_ATENCION = "2"
TARGET_CATEGORIA = "13"


def _int_env(name, default):
    return int(os.environ.get(name, "").strip() or default)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dian_booker", description=__doc__)
    parser.add_argument("--trigger", choices=("found", "watch", "manual"), default="manual",
                        help="found: send the live link at once; otherwise when the city appears")
    parser.add_argument("--dry-run", action="store_true",
                        default=os.environ.get("BOOKER_DRY_RUN", "") in ("1", "true"),
                        help="go through the captcha and the form but never click Agendar")
    parser.add_argument("--local", action="store_true",
                        help="use this computer's screen instead of Xvfb + noVNC + tunnel")
    parser.add_argument("--service", default=os.environ.get("BOOKER_SERVICE", ""),
                        help="tramite substring to choose among the city's tramites (e.g. vehiculos);"
                             " a case's own `service` wins")
    parser.add_argument("--case", default=os.environ.get("BOOKER_CASE", "").strip() or DEFAULT_ID,
                        help="case id from the cases JSON (default: the BOOKER_* person, DIAN_CITY)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    try:
        case = cases_mod.find(settings, args.case)
        profile = Profile.from_env() if case.id == DEFAULT_ID else Profile.from_case(case)
    except (CasesError, ProfileError) as e:
        log(f"Not starting: {e}.")
        if args.trigger != "manual":  # a dispatch the monitor thought would book: say why not
            build_notifier().send(Message(f"⚠️ El bot que agenda no arrancó: {e}.", urgent=True))
        return 2
    # From here on the booker works for this case only: its city, its booking record.
    settings = replace(settings, city=case.city,
                       booking_file=cases_mod.booking_file(settings, case.id))
    if case.city not in ALLOWED_CITIES and not args.dry_run:
        log(f"Not starting: real bookings are only for {sorted(ALLOWED_CITIES)} "
            f"(city={case.city!r}). Other cities can only be tested with --dry-run.")
        return 2
    if (settings.tipo_atencion, settings.categoria) != (TARGET_ATENCION, TARGET_CATEGORIA) \
            and not args.dry_run:
        log(f"Not starting: real bookings are only for Videoatención / Devoluciones "
            f"({settings.label}). Others can only be tested with --dry-run.")
        return 2
    log(f"Starting {case.id} ({args.trigger}{', dry run' if args.dry_run else ''}): "
        f"{settings.label} / {case.city_name}")
    notifier = build_notifier([case.telegram_chat] if case.telegram_chat else None)
    result = Booker(settings, profile, notifier, trigger=args.trigger, case=case,
                    dry_run=args.dry_run, local=args.local,
                    service_hint=case.service or args.service,
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
