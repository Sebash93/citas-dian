# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A watcher for DIAN appointment slots (agendamiento.dian.gov.co): Persona Natural → Videoatención → Devoluciones, for the cities of the **cases** (`cases.py`: one person + city each; without a cases JSON, one "default" case for Medellín with the `BOOKER_*` person). The monitor runs nonstop on Render (`render.yaml`, fallback: GitHub Actions cron) and alerts through Telegram. The booker runs on GitHub Actions. An optional booker (`dian_booker`) drives the booking wizard up to the reCAPTCHA. The user solves the captcha through a live-browser link, then the booker finishes.

Hard constraints:
- A human always solves the reCAPTCHA. Never add captcha-solving services or audio/AI solvers, and never auto-solve the captcha in any other way.
- Personal data (BOOKER_* secrets, the personal fields of the cases JSON) and the live-browser link must never reach logs or uploaded artifacts. The repo is public. Use `Message(private=True)` for link messages. Screenshots go only to Telegram. The monitor loads cases with `personal=False` and never holds personal data; case ids (public in logs and run names) are validated to carry none. `CasesError`/`ProfileError` texts name fields, never values.
- Cities are limited to `cases.ALLOWED_CITIES` (Bogotá, Bucaramanga, Cali, Medellín: the seccionales where DIAN requires an appointment for manual devoluciones). Alerts fire only for cities of enabled cases. Openings in other cities are logged and captured, never alerted.
- A booker works for one case and books only a tramite whose name contains that case's city word (`mentions_city`), with no default or fallback option. DIAN names the city differently in the API (`“Medellín - …”`) and in the wizard (`Medellín “…”`), so match the word, never a format. `Wizard.check_city()` re-checks before the captcha and before Agendar, `_record` only records bookings in the case's city, and `dian_booker` refuses real (non-dry) runs for a city outside `ALLOWED_CITIES`.
- Real bookings are always Videoatención / Devoluciones: `dian_booker` refuses non-dry runs with any other `DIAN_TIPO_ATENCION`/`DIAN_CATEGORIA` (those are only for dry-run wizard checks in `booker-test.yml`).
- Keep request volume close to a person refreshing the page (each check is about 2 requests). Change polling rates only in `cadence.py`.

## Commands

```bash
pip install -r requirements.txt                       # only dependency: requests
python3 -m unittest discover -s tests                 # all tests (offline, uses fixtures)
python3 -m unittest tests.test_monitor.MonitorTest.test_realerts_are_capped   # single test
PYTHONPATH=src python3 -m dian_monitor --once         # one live check, ignores cadence and pauses
PYTHONPATH=src python3 -m dian_monitor --budget 300   # poll live for 5 minutes
PYTHONPATH=src python3 -m dian_monitor --forever      # what Render runs: back-to-back runs, never exits
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
- **`cases.py`**: loads the cases JSON (`DIAN_CASES_JSON` or `DIAN_CASES_FILE`), validates it, `booking_file()` / `booked_artifact()` per case (the default case keeps the old `booking.json` / `booking-booked`).
- **`check.py`**: classifies a colas response for a list of cities as `NONE`/`FOUND`, with `by_city`. City matching uses only the city prefix of the tramite `Nombre` (`"Cali - Solicitud…"` → `cali`, `"Bogotá D.C. - …"` → `bogota`), after removing accents and HTML. Other fields such as Requisitos mention several cities, so matching on the whole item causes false positives (see the `colas_cali_real.json` fixture). Any unexpected shape raises `SchemaChanged`.
- **`monitor.py`**: one invocation polls on the cadence (with ±15% jitter) until the time budget runs out. Openings are tracked per city in `state["openings"]`, booker decisions per case in `state["booker_decisions"]`. It handles:
  - one alert per open city, listing its cases still waiting, and re-alerts while it stays open (`realert_seconds`, `max_realerts`); a city whose cases are all booked gets one note instead;
  - the "broken bot" alert after N consecutive failures, plus a recovery message;
  - exponential pauses after blocks (10, 20, 40… min, capped at 120), stored as `blocked_until` so later runs skip;
  - a daily heartbeat;
  - silent "watch" bookers, one per case whose city is not open, when a tramite opens that was not open at the last check (`state["last_open"]`), since DIAN releases cities in waves. A city that stays open can't keep bookers running. Slowness is not used as a signal: over about a week it preceded Medellín in only 1 of about 20 booker runs.

  Raw responses for openings are written to `captures/`. `run()` returns a marker string such as `MONITOR_OK_NONE` or `MONITOR_BLOCKED`.
- **`state.py`**: a JSON state file (`state/state.json`) that persists between runs. `load()` merges stored data over `empty()`, so a new key only needs a default in `empty()`. `_migrate()` turns the pre-cases top-level Medellín opening into `openings["medellin"]`.
- **`cadence.py`**: polling interval by Bogotá weekday and time (UTC−5, no DST).
- **`config.py`**: frozen `Settings` with DIAN's own codes (tipo_persona, tipo_atencion, categoria). Every field can be overridden through a `DIAN_*` env var.
- **`notify.py`**: alert texts (in Spanish) and the notifier. `TelegramNotifier` is used when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (comma-separated) are set. Otherwise `ConsoleNotifier` is used. Never log the Telegram API URL, because it contains the token.

### Booker (`src/dian_booker/`)

- **`monitor.py` starts the bookers** when `DIAN_BOOKER=1`: one per case (`booker.start(case, trigger)`), never while that case has a booking recorded (`booker.booked(case)`), and at most `booker.capacity` at once (`DIAN_MAX_PARALLEL_BOOKERS`, default 4; cases of open cities go before watches). `build_booker()` picks how:
  - `GitHubBooker` (`remote_booker.py`, when `DIAN_BOOKER_REPO` is set, i.e. on Render) dispatches `booker.yml` with inputs `case` and `trigger` through the GitHub API with `GITHUB_DISPATCH_TOKEN`. Runs are titled `Booker <case> <trigger>` (`run-name`) and matched to cases by that title (untitled old runs are the default case); one runs listing is cached for 10 s so a check costs one call for all cases. `booked(case)` checks for the case's `booking-booked-<case>` artifact on every check, with no local cache, so deleting the artifact re-enables booking.
  - While a city is open, `_maybe_book` runs for each of its cases on **every** check, so a booker that ended or never started is replaced within one check. Its outcome (`off`, `booked`, `started`, `running` with the GitHub run status, `waiting` at the cap, `error`) is logged and emitted as `booker_decision` (with `case`, `city`, `opening`) whenever it changes. "error" sends a Telegram alert. The report lists these under each opening and flags "NO BOOKER during this opening". Dispatch errors never stop monitoring; they must never contain the token.
  - `BookerProcess` (GitHub fallback `monitor.yml`) runs `python -m dian_booker --case <id> --trigger found|watch` beside the monitor, one at a time (shared display and CDP port); `run()` waits for it before returning.
- **`__main__.py`:** `--case` (or `BOOKER_CASE`) picks the case with `cases.find()`, then replaces `settings.city` and `settings.booking_file` with the case's, so `Booker`/`Wizard` work on one case only. An unknown or disabled case exits 2 and, for a dispatched run, says so on Telegram. A case's `service` beats `BOOKER_SERVICE`; its `telegram_chat` replaces `TELEGRAM_CHAT_ID`.
- **`remote.py`:** sets up the remote screen: Xvfb at 412×900 (phone size) → x11vnc (random 8-character password) → websockify/noVNC → Cloudflare quick tunnel. The link is `novnc_url(...)`. On GitHub, `.github/scripts/booker-setup.sh` installs the packages in the background at job start and touches `BOOKER_READY_FILE` when done. Xvfb and Chrome are preinstalled on the runner, so the browser starts at once and everything after Xvfb comes up in a background thread; `url()` blocks until the tunnel is up.
- **`booker.py`:** launches the system Chrome as a normal (not automation-flagged) window and connects Playwright to it over CDP (`connect_over_cdp`).
  - With `--trigger found`, it sends the "prepárate" link as soon as the tunnel is up, from a background thread, so the wizard never waits for the tunnel. With `watch`, it sends the link only when the case's city appears. Every message names the case (`Case.who`). "Toca el captcha" always waits for the link (`_wait_for_link`).
  - It sends "toca el captcha" at PasoTres, then polls `window.captchaValido`.
  - If something fails after the link is sent, it keeps the browser open (`BOOKER_HOLD_MINUTES`) so the human can finish by hand.
  - Results: `BOOKED`, `DRY_RUN_OK`, `NOT_OPEN` (target never offered), `SLOT_GONE` (`SlotUnavailable`: dates or slot taken first), `UNRESPONSIVE` (DIAN's page timed out; PasoUno retries until the wait ends), `CAPTCHA_TIMEOUT`, `FAILED`. Only `FAILED` exits 1, so a red GitHub run always means the booker itself broke.
  - The browser is held open for the human only after a failure from the captcha on (`HOLD_STAGES`). Earlier failures exit at once so the next alert can start a new booker.
  - The case's `service`, else `BOOKER_SERVICE` (repo variable for `booker.yml`, set to `vehiculos`), is a comma-separated preference among the city's open tramites; if none matches, the first tramite of the city is booked. PasoUno logs which of the city's tramites were offered and which was chosen.
- **`wizard.py`:** controls are `[nombre="..."]` (and `div[pantalla="PasoTres"][nombre=...]`), and the dropdowns are native `<select>`s. The date is a Kendo popup: open it with `btnFecha`, change months with `kendoCalendar.navigateToFuture()` because the arrows can't be clicked at phone width, then click the first `td` without `k-disabled`. Steps were verified live up to the captcha on 2026-10-26. The steps after the captcha follow the site's scripts and are unverified until a dry run gets past the captcha.

### Event log (`events.py`)

`EventLog.emit(kind, **fields)` appends to `state/events.jsonl` (`DIAN_EVENTS_FILE`). Both the monitor and the booker subprocess write to it in append mode. Only the monitor trims it, at the end of `run()`, after the booker has finished. `emit` never raises.
- **Monitor kinds:** `run` (one per run, with counts and latency), `opening` and `closed` (with `city`), `failure`, `blocked`, `other_city`, `booker_decision` (with `case`, `city`), `crash`. Events from before cases have no `city` and are read as Medellín.
- **Booker kinds:** `booker_start`, `booker_stage`, `dian_step` (timed from Playwright request events), `booker_link_sent`, `booker_end`.
- **Reports:** `events.report()` backs `python -m dian_monitor --report [FILE] [--days N]`, and `scripts/report.sh` downloads the newest `events-<run_id>` artifact first.
- **Privacy:** events are uploaded from a public repo, so never put personal data or the link in them. Free text from the booker goes through `Profile.scrub()`.

`Monitor` takes `client`, `clock`, `sleep`, `rand` and `booker` as constructor arguments. Test helpers must point `state_file`, `events_file` and `booking_file` at a temp dir. Tests replace them with `FakeClient`/`FakeSession`, a fake `Clock` and fixtures in `tests/fixtures/`, so tests never touch the network.

## Deployment

The monitor and booker deploy separately from this one repo:
- **Render (monitor):** `render.yaml` Blueprint, a worker running `python -m dian_monitor --forever` with a disk at `/var/data` for state, events and captures. `buildFilter` redeploys only on monitor changes. `dian_monitor` must not import `dian_booker` or Playwright, since Render installs only `requirements.txt`.
  - `run_forever()` chains `run()`s of `run_budget_seconds` each (one `run` event per chunk), sleeps through block pauses, and on an unexpected exception logs it, emits `crash`, pings `/fail` and retries after 60 s.
  - `health.py` pings `DIAN_HEALTHCHECK_URL` (healthchecks.io) after each check and during waits, which `_wait()` slices into ≤5 min. Never log that URL.
- **GitHub Actions (booker and fallback):**
  - `booker.yml` is dispatched by the Render monitor (inputs `trigger`, `case`), with concurrency group `dian-booker-<case>`. The case id reaches the script only through `BOOKER_CASE` env, never interpolated into `run:`.
  - `monitor.yml` runs on cron every 5 minutes but only polls when the repo variable `DIAN_GITHUB_MONITOR=1` (compare with `'1'`: an unset variable equals `'0'` in Actions expressions). Each run polls for about 19 minutes (`run_budget_seconds=1140`). The `dian-monitor` concurrency group queues runs so coverage stays continuous despite cron delays.
- State lives in the Actions cache: it is restored by the `dian-state-` prefix and saved under a unique key on every run.
- On GitHub, `captures/` is uploaded as `captures-<run_id>` artifacts (on Render it is `/var/data/captures`). When DIAN's response format is in doubt, check these first.
- `keepalive.yml` re-enables the schedule twice a month.
- Booker settings:
  - `DIAN_BOOKER=1` turns the booker on (Render env for the Render monitor, repo variable for the fallback). The repo variable `BOOKER_DRY_RUN=1` makes it stop before Agendar.
  - Secrets: `BOOKER_CASES` (the full cases JSON, one line; mapped to `DIAN_CASES_JSON`), or for the default case `BOOKER_DOC_NUMBER`, `BOOKER_FULL_NAME`, `BOOKER_EMAIL`, `BOOKER_PHONE`, and optionally `BOOKER_DOC_TYPE`. Render's `DIAN_CASES_JSON` needs only the routing fields.
  - `cases.example.json` is fake data; real cases go in git-ignored `cases.json`, never committed.
  - `booker-test.yml` runs the booker by hand on any category, as a dry run by default.
- The repo must stay public for free Actions minutes, so logs must not contain personal data.
