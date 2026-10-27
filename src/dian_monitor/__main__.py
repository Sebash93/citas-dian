"""python -m dian_monitor [--once | --forever] [--budget SECONDS] [--telegram-chats | --telegram-test | --report]"""
import argparse
import os
from datetime import timedelta

from . import cadence, events

from .config import Settings
from .monitor import Monitor, log
from .notify import build_notifier, find_chat_ids, test_message


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dian_monitor", description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="single check now, ignoring cadence and pauses")
    parser.add_argument("--forever", action="store_true",
                        help="never stop (always-on hosting such as Render)")
    parser.add_argument("--budget", type=int, default=None,
                        help="seconds to keep polling (default: DIAN_RUN_BUDGET_SECONDS)")
    parser.add_argument("--telegram-chats", action="store_true",
                        help="list chat ids that messaged the bot (needs TELEGRAM_BOT_TOKEN)")
    parser.add_argument("--telegram-test", action="store_true",
                        help="send a test message to TELEGRAM_CHAT_ID")
    parser.add_argument("--report", nargs="?", const="", metavar="EVENTS_FILE",
                        help="summarize the event log (default: DIAN_EVENTS_FILE) and exit")
    parser.add_argument("--days", type=int, default=None, help="with --report: only the last N days")
    args = parser.parse_args(argv)

    if args.report is not None:
        path = args.report or Settings.from_env().events_file
        since = cadence.now_bogota() - timedelta(days=args.days) if args.days else None
        print(events.report(events.read(path), since=since))
        return 0

    if args.telegram_chats:
        token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
        bot, webhook, chats = find_chat_ids(token)
        print(f"Token belongs to @{bot['username']} (https://t.me/{bot['username']})")
        if webhook:
            print(f"A webhook is set ({webhook}); it swallows messages. Remove it with "
                  "deleteWebhook or create a new bot.")
        if not chats:
            print(f"No chats yet: open https://t.me/{bot['username']}, tap Start, send 'hola', "
                  "and retry.")
        for chat_id, desc in chats:
            print(f"{chat_id}\t{desc}")
        return 0
    if args.telegram_test:
        return 0 if build_notifier().send(test_message()) else 1

    settings = Settings.from_env()
    log(f"Watching: {settings.label}")
    monitor = Monitor(settings, build_notifier())
    if args.forever:
        monitor.run_forever()
        return 0
    print(monitor.run(budget_seconds=args.budget, once=args.once), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
