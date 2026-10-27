"""python -m dian_booker [--trigger found|posible|manual] [--dry-run] [--local]"""
import argparse
import os

from dian_monitor.config import Settings
from dian_monitor.notify import build_notifier

from .booker import EXPECTED, Booker, log
from .profile import Profile, ProfileError


def _int_env(name, default):
    return int(os.environ.get(name, "").strip() or default)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dian_booker", description=__doc__)
    parser.add_argument("--trigger", choices=("found", "posible", "manual"), default="manual",
                        help="found: send the live link at once; otherwise when the city appears")
    parser.add_argument("--dry-run", action="store_true",
                        default=os.environ.get("BOOKER_DRY_RUN", "") in ("1", "true"),
                        help="go through the captcha and the form but never click Agendar")
    parser.add_argument("--local", action="store_true",
                        help="use this computer's screen instead of Xvfb + noVNC + tunnel")
    parser.add_argument("--service", default=os.environ.get("BOOKER_SERVICE", ""),
                        help="tramite substring, for categories whose names carry no city")
    args = parser.parse_args(argv)

    try:
        profile = Profile.from_env()
    except ProfileError as e:
        log(f"Not starting: {e}.")
        return 2
    settings = Settings.from_env()
    log(f"Starting ({args.trigger}{', dry run' if args.dry_run else ''}): {settings.label}")
    result = Booker(settings, profile, build_notifier(), trigger=args.trigger,
                    dry_run=args.dry_run, local=args.local, service_hint=args.service,
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
