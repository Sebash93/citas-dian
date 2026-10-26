# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A watcher for DIAN appointment slots (agendamiento.dian.gov.co): Persona Natural → Videoatención → Devoluciones, **Medellín only**. It runs on GitHub Actions and alerts through Telegram. An optional booker (`dian_booker`) drives the booking wizard up to the reCAPTCHA. The user solves the captcha through a live-browser link, then the booker finishes.

Hard constraints:
- A human always solves the reCAPTCHA. Never add captcha-solving services or audio/AI solvers, and never auto-solve the captcha in any other way.
- Personal data (BOOKER_* secrets) and the live-browser link must never reach logs or uploaded artifacts. The repo is public. Use `Message(private=True)` for link messages. Screenshots go only to Telegram.
- Alerts must fire only for Medellín. Openings in other cities are logged and captured, never alerted.
- Keep request volume close to a person refreshing the page (each check is about 2 requests). Change polling rates only in `cadence.py`.

## Commands

```bash
pip install -r requirements.txt                       # only dependency: requests
python3 -m unittest discover -s tests                 # all tests (offline, uses fixtures)
python3 -m unittest tests.test_monitor.MonitorTest.test_realerts_are_capped   # single test
PYTHONPATH=src python3 -m dian_monitor --once         # one live check, ignores cadence and pauses
PYTHONPATH=src python3 -m dian_monitor --budget 300   # poll live for 5 minutes
PYTHONPATH=src python3 -m dian_monitor --telegram-chats   # list chat ids (needs TELEGRAM_BOT_TOKEN)
PYTHONPATH=src python3 -m dian_monitor --telegram-test    # send a test alert
PYTHONPATH=src python3 -m dian_monitor --report state/events.jsonl --days 7   # summarize the event log
scripts/report.sh 7                                    # same, from the newest GitHub artifact

# Booker (needs: pip install -r requirements-booker.txt, Google Chrome, BOOKER_* env vars)
PYTHONPATH=src python3 -m dian_booker --local --dry-run --trigger found        # your own screen, stops before Agendar
DIAN_TIPO_ATENCION=1 DIAN_CATEGORIA=7 BOOKER_HEADLESS=1 BOOKER_CAPTCHA_MINUTES=1 \
  PYTHONPATH=src python3 -m dian_booker --local --dry-run --trigger found --service "persona natural"
  # wizard check on an open category (presencial RUT); waits at the captcha, then times out
```

No linter or build step is configured. CI (`tests.yml`) runs the tests on Python 3.12. Tests never import Playwright: keep its import inside `Booker.run`.

## Architecture (`src/dian_monitor/`)

The flow is `__main__` → `Monitor.run` → `DianClient.find_colas` → `check.evaluate` → `notify` → `state.save`.

- **`client.py`**: read-only HTTP client for the Ciel "C-Media WebPlayer" (ASP.NET WebForms) backend. It scrapes a single-use `anticsrf` token from the page. Every later call rotates the token through the `X-token` response header. `ObtenerConfiguracion` (about 500 KB) is fetched once per session. The availability lookup is a `ValidadorValidar` POST with `DetalleAdicional = manejadorEncontroColas`. Responses arrive as `{"d": "<json string>"}`. Errors form a hierarchy that drives monitor behavior:
  - `DianError`: transient.
  - `Blocked`: 403/429 or a challenge page.
  - `SchemaChanged`: the response no longer matches what the code expects and needs a human.
  - `TimedOut`: DIAN did not answer within 90 s.
- **`check.py`**: classifies a colas response as `NONE`/`FOUND`. City matching uses only the city prefix of the tramite `Nombre` (`"Cali - Solicitud…"` → `cali`), after removing accents and HTML. Other fields such as Requisitos mention several cities, so matching on the whole item causes false positives (see the `colas_cali_real.json` fixture). Any unexpected shape raises `SchemaChanged`.
- **`monitor.py`**: one invocation polls on the cadence (with ±15% jitter) until the time budget runs out. It handles:
  - re-alerts while a slot stays open (`realert_seconds`, `max_realerts`);
  - the "broken bot" alert after N consecutive failures, plus a recovery message;
  - exponential pauses after blocks (10, 20, 40… min, capped at 120), stored as `blocked_until` so later runs skip;
  - a daily heartbeat;
  - "posible" alerts when DIAN is slow (≥10 s) or times out, with a cooldown. DIAN slows down when slots open, so slowness is treated as a signal.

  Raw responses for openings are written to `captures/`. `run()` returns a marker string such as `MONITOR_OK_NONE` or `MONITOR_BLOCKED`.
- **`state.py`**: a JSON state file (`state/state.json`) that persists between runs. `load()` merges stored data over `empty()`, so a new key only needs a default in `empty()`.
- **`cadence.py`**: polling interval by Bogotá weekday and time (UTC−5, no DST).
- **`config.py`**: frozen `Settings` with DIAN's own codes (tipo_persona, tipo_atencion, categoria). Every field can be overridden through a `DIAN_*` env var.
- **`notify.py`**: alert texts (in Spanish) and the notifier. `TelegramNotifier` is used when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (comma-separated) are set. Otherwise `ConsoleNotifier` is used. Never log the Telegram API URL, because it contains the token.

### Booker (`src/dian_booker/`)

- **`monitor.py` starts the booker:** `BookerProcess` runs `python -m dian_booker --trigger found|posible` when `DIAN_BOOKER=1`. It starts on every opening alert and on "posible" alerts, runs one process at a time, and doesn't start once `state/booking.json` says booked. `run()` waits for the booker before returning.
- **`remote.py`:** sets up the remote screen: Xvfb at 412×900 (phone size) → x11vnc (random 8-character password) → websockify/noVNC → Cloudflare quick tunnel. The link is `novnc_url(...)`. On GitHub, `.github/scripts/booker-setup.sh` installs the packages in the background at job start and touches `BOOKER_READY_FILE` when done.
- **`booker.py`:** launches the system Chrome as a normal (not automation-flagged) window and connects Playwright to it over CDP (`connect_over_cdp`).
  - With `--trigger found`, it sends the "prepárate" link at once. Otherwise it sends the link only when Medellín appears.
  - It sends "toca el captcha" at PasoTres, then polls `window.captchaValido`.
  - If something fails after the link is sent, it keeps the browser open (`BOOKER_HOLD_MINUTES`) so the human can finish by hand.
- **`wizard.py`:** controls are `[nombre="..."]` (and `div[pantalla="PasoTres"][nombre=...]`), and the dropdowns are native `<select>`s. The date is a Kendo popup: open it with `btnFecha`, change months with `kendoCalendar.navigateToFuture()` because the arrows can't be clicked at phone width, then click the first `td` without `k-disabled`. Steps were verified live up to the captcha on 2026-10-26. The steps after the captcha follow the site's scripts and are unverified until a dry run gets past the captcha.

### Event log (`events.py`)

`EventLog.emit(kind, **fields)` appends to `state/events.jsonl` (`DIAN_EVENTS_FILE`). Both the monitor and the booker subprocess write to it in append mode. Only the monitor trims it, at the end of `run()`, after the booker has finished. `emit` never raises.
- **Monitor kinds:** `run` (one per run, with counts and latency), `opening`, `closed`, `failure`, `blocked`, `slow`, `other_city`, `booker_launch`.
- **Booker kinds:** `booker_start`, `booker_stage`, `dian_step` (timed from Playwright request events), `booker_link_sent`, `booker_end`.
- **Reports:** `events.report()` backs `python -m dian_monitor --report [FILE] [--days N]`, and `scripts/report.sh` downloads the newest `events-<run_id>` artifact first.
- **Privacy:** events are uploaded from a public repo, so never put personal data or the link in them. Free text from the booker goes through `Profile.scrub()`.

`Monitor` takes `client`, `clock`, `sleep`, `rand` and `booker` as constructor arguments. Test helpers must point `state_file`, `events_file` and `booking_file` at a temp dir. Tests replace them with `FakeClient`/`FakeSession`, a fake `Clock` and fixtures in `tests/fixtures/`, so tests never touch the network.

## Deployment (GitHub Actions)

- `monitor.yml` runs on cron every 5 minutes, and each run polls for about 19 minutes (`run_budget_seconds=1140`). The `dian-monitor` concurrency group queues runs so coverage stays continuous despite cron delays.
- State lives in the Actions cache: it is restored by the `dian-state-` prefix and saved under a unique key on every run.
- `captures/` is uploaded as `captures-<run_id>` artifacts. When DIAN's response format is in doubt, check these first.
- `keepalive.yml` re-enables the schedule twice a month.
- Booker settings:
  - The repo variable `DIAN_BOOKER=1` turns the booker on, and `BOOKER_DRY_RUN=1` makes it stop before Agendar.
  - Secrets: `BOOKER_DOC_NUMBER`, `BOOKER_FULL_NAME`, `BOOKER_EMAIL`, `BOOKER_PHONE`, and optionally `BOOKER_DOC_TYPE`.
  - `booker-test.yml` runs the booker by hand on any category, as a dry run by default.
- The repo must stay public for free Actions minutes, so logs must not contain personal data.
