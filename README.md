# citas-dian

Watches [agendamiento.dian.gov.co](https://agendamiento.dian.gov.co/?recurso=CitasDIAN) for
**Persona Natural → Videoatención → Devoluciones** slots in the cities of your cases
(Bogotá, Bucaramanga, Cali, Medellín; Medellín only without cases) and alerts as soon as one
opens.

By default it only notifies you. An optional **booker** (off unless `DIAN_BOOKER=1`) takes
you as far as it can: it fills in the wizard, sends you a live-browser link on Telegram, and
waits at the reCAPTCHA. **You solve the captcha yourself**, from your phone, and the bot then
finishes the booking. It never solves the captcha automatically.

## How it works

It talks to the same internal endpoint the page uses (no browser):

1. `GET /?recurso=CitasDIAN` → single-use `anticsrf` token (later rotated through the
   `X-token` response header).
2. `POST /Player.aspx/ObtenerConfiguracion` → encrypted session config (once per run).
3. `POST /Player.aspx/ValidadorValidar` with `manejadorEncontroColas` and
   `[cita, "Nombre", 1 (Natural), 13 (Devoluciones), 2 (Video)]` → tramites with open slots.
   Each item is searched for "medellin" (accent- and case-insensitive).

Every check ends as `NONE`, `FOUND`, a transient error, `SchemaChanged` (DIAN changed
something: alert after 3 in a row) or `Blocked` (403/429: stop and pause 10, 20, 40… min).

## Cadence (Bogotá time)

| When | Every |
|---|---|
| Fri 6:30–17:00 | ~20 s |
| Mon–Thu 7:00–17:00 | ~35 s |
| Weekdays 7:00–19:00 | ~75 s |
| Weekdays 6–7, 19–21 | 5 min |
| Nights, weekends | 15 min |

Each check costs 2 requests (about what a person refreshing the page would do). Edit
`src/dian_monitor/cadence.py` to tune it; openings are recorded in the state `history`.

## Run locally

```bash
pip install -r requirements.txt
PYTHONPATH=src python3 -m dian_monitor --once        # one check
PYTHONPATH=src python3 -m dian_monitor --budget 300  # poll for 5 minutes
python3 -m unittest discover -s tests                # tests (no network)
```

## Deployment: monitor on Render, booker on GitHub

The two parts live in this repo but deploy separately:

| Part | Where | Deployed by | Runs |
|---|---|---|---|
| Monitor (`src/dian_monitor`) | Render background worker | `render.yaml` | `python -m dian_monitor --forever` |
| Booker (`src/dian_booker`) | GitHub Actions | `.github/workflows/booker.yml` | `python -m dian_booker --trigger …`, started by the monitor |

GitHub's cron is best effort and often starts only a few runs a day, so the monitor runs
nonstop on Render. Render redeploys it on pushes that touch `src/dian_monitor/`,
`requirements.txt` or `render.yaml`. The booker always runs the code on `main`.

### Render (monitor)

1. **Health check:** on [healthchecks.io](https://healthchecks.io) (free), create a check with
   period 10 min and grace 10 min. Add your Telegram or email as its notification. Copy the
   ping URL.
2. **GitHub token:** github.com → Settings → Developer settings → Fine-grained tokens. Choose
   **only this repository** and the permission **Actions: Read and write**. Copy the token.
3. **Render:** New → **Blueprint** → pick this repo. It reads `render.yaml`: a Starter worker
   in Virginia with a 1 GB disk for the state (about $7.25/month). When asked, paste
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GITHUB_DISPATCH_TOKEN`, `DIAN_HEALTHCHECK_URL`
   and `DIAN_BOOKER` (`1` to start the booker on openings, `0` for alerts only).
4. Watch the logs for a few `Check: NONE` lines. That confirms DIAN accepts Render's IP.
5. GitHub's fallback monitor stays off unless you turn it on with
   `gh variable set DIAN_GITHUB_MONITOR --body 1`.

The monitor pings the health check after each check and at least every 5 minutes while it
waits. If the process or Render goes down, healthchecks.io alerts you. If it crashes on an
unexpected error, it logs the error, pings `/fail` and retries a minute later. Render also
restarts it if the process exits.

Reports on Render (Shell tab):
`python -m dian_monitor --report /var/data/state/events.jsonl --days 7`.

### GitHub Actions

- `booker.yml` is started by the monitor through the GitHub API (see Booker below).
- `monitor.yml` is the fallback monitor. It starts every 5 minutes and each run polls for about
  19 minutes, with a concurrency group queuing the next one. State is kept in the Actions
  cache, and raw responses for openings are uploaded as `captures-*` artifacts. Its schedule
  only polls when the repo variable `DIAN_GITHUB_MONITOR=1`; otherwise runs are skipped. Manual runs (**Actions → Monitor citas DIAN → Run
  workflow**) always work.
- `keepalive.yml` re-enables the schedule twice a month (GitHub disables it after 60 days
  without repository activity).

The repo must be public for unlimited free Actions minutes. Logs contain no personal data.

## Cases: several people and cities, from Airtable

The people to book live in the Airtable table **Cases** (base "Gestión de Casos y Citas"). People
sign up with the Spanish form **"Solicitud de cita DIAN – Devolución IVA vehículos"**. The monitor
alerts for every city that has a pending case and starts one booker per case when its city
opens. Only the cities where DIAN requires an appointment to file a devolución by hand are
accepted: **Bogotá, Bucaramanga, Cali and Medellín** (everywhere else, documents go by email).
Every case is for the same tramite, **IVA vehículos eléctricos o híbridos**: the general
devolución tramite is never booked, and its openings are only logged.

| Field | Who fills it | Notes |
|---|---|---|
| `ID` | Airtable (autonumber) | The case is `case-<ID>` in logs and GitHub runs |
| `Full name`, `City`, `Document number`, `Email`, `Phone` | The person (form) | Document type is always cédula de ciudadanía |
| `Status` | You / the bot | Empty or **Pending**: look for an appointment. **Paused**: ignore. **Booked**: set by the bot |
| `Appointment`, `Booked at` | The bot | Written after a real booking |

- **When it reads:** the monitor reads only `ID`, `City` and `Status` (never the personal
  fields) at startup and after **6:00, 11:00 and 14:00** Bogotá (`DIAN_CASES_REFRESH_HOURS`).
  To apply a change at once, restart the Render service.
- **Booker reads:** a booker reads its own record only when it needs the person, at once for an
  opening, or when its city appears for a watch. After a real booking it sets `Status`,
  `Appointment` and `Booked at`.
- **Bad data:** a record with missing or invalid data (no city, a city outside the four, a
  phone that isn't 10 digits…) is skipped and reported once on Telegram by its `case-<ID>`.
- **Airtable down:** if Airtable can't be read, the monitor keeps the last good copy
  (`DIAN_CASES_CACHE_FILE`, id/city/status only), retries after 10–60 min and tells you on
  Telegram.
- **Quota:** the free plan allows 1,000 API calls a month. Expected use is about 250; the
  report (`--report`) counts every call.

For local runs, copy `.env.example` to `.env` (it points at the test base "Citas DIAN –
Pruebas", fake people only) and check the connection with
`PYTHONPATH=src python3 -m dian_monitor --cases`. Without Airtable, everything works as before: one
"default" case for `DIAN_CITY` (Medellín) with the `BOOKER_*` secrets.

When a city opens, you get one alert for it listing the cases still waiting. A city whose cases
all have a booking recorded is not alerted (you get one note instead). At most
`DIAN_MAX_PARALLEL_BOOKERS` (default 4) booker runs go at once; cases of an open city start
before silent watches. Each captcha message says which case it is for.

## Booker (optional)

When a case's city opens, the monitor starts that case's booker. When any new tramite opens,
it also starts one silently for each case whose city is not open, because DIAN releases cities
in waves: for up to 12 minutes the booker checks for its city about every 25 s and sends you
nothing unless it appears. It never books another city (`DIAN_BOOKER_ON_OTHER_CITY=0` turns
this off).
On Render (`DIAN_BOOKER_REPO` set), it dispatches `booker.yml` through the GitHub API; the
run usually starts within seconds. In GitHub's fallback `monitor.yml`, it runs
`python -m dian_booker` on the same runner. Either way:

1. It sets up a phone-sized virtual screen (Xvfb → x11vnc → noVNC) behind a Cloudflare quick
   tunnel and opens Chrome on the screen.
2. It sends **"PREPÁRATE"** with the link as soon as the tunnel is up, so you can open it
   while the bot works. The wizard doesn't wait for the tunnel.
3. It goes through Persona Natural → Videoatención → Devoluciones → the case's city, then picks the
   earliest date and time and your document type. Each step takes 5–10 s when slots are open.
4. It sends **"TOCA EL CAPTCHA"**. You tap "No soy un robot" in the live view.
5. It types your document number, fills in or confirms your details, accepts the data policy,
   clicks Agendar, and sends you the confirmation and a screenshot.

If anything fails after the link was sent, the browser stays open 10 minutes so you can finish
by hand. Add more people to `TELEGRAM_CHAT_ID` (comma-separated) so that anyone you trust can
tap the captcha.

The booker only ever picks a tramite whose name contains **its case's city**. There is no
default option: if no tramite of that city is offered, it books nothing (`NOT_OPEN`). It
checks the city again before the captcha and before Agendar, and refuses a real booking
outside the four appointment cities. It also books only the tramite of `DIAN_SERVICE`
(`vehiculos`: "Vehículos eléctricos o Híbridos"), with no fallback to another tramite.

While a city is open, the monitor checks on every poll that each of its cases has a booker and starts
a new one if the last one ended. Each change ("started", "running (GitHub run … queued)",
"booked", "off", "error") is logged on Render and in the event log, and the report lists them
under each opening, flagging any opening that had no booker.

One booker runs per case at a time. After a real booking in the case's city (by the bot, or
by you in the live browser) the workflow uploads an empty `booking-booked-<case>` artifact
(`booking-booked` for the default case). While it exists, the monitor starts no more bookers
for that case. Delete the artifact on GitHub to book that case again; the monitor checks it on
every poll. Bookings for another city are never recorded.

If DIAN's page doesn't load, the booker retries instead of failing and reports
`UNRESPONSIVE`. A failure before the captcha closes the browser at once, so the next alert
can start a new booker; from the captcha on, the browser stays open for you to finish.

Setup:
- On Render: `DIAN_BOOKER=1` and `GITHUB_DISPATCH_TOKEN`. For the GitHub fallback: repo
  variable `DIAN_BOOKER=1`. Repo variable `BOOKER_DRY_RUN=1` stops before Agendar in both.
- Cases (see Cases): Render env `AIRTABLE_TOKEN`; GitHub secret `AIRTABLE_TOKEN` and repo
  variables `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE`. The token is a Personal Access Token with only
  `data.records:read` and `data.records:write` on the base. For the single default case, secrets `BOOKER_DOC_NUMBER`,
  `BOOKER_FULL_NAME`, `BOOKER_EMAIL`, `BOOKER_PHONE`, and optionally `BOOKER_DOC_TYPE`
  (default "Cédula de ciudadanía").
- To test: **Actions → Booker test run** walks any open category as a dry run and sends you
  the link. Give it a `case` (e.g. `case-3`) to use that Airtable case's person and city.
- The GitHub fallback monitor runs one booker at a time, and only the default case's booking
  reaches Render (other cases' bookings stay in its own state).

Locally: `pip install -r requirements-booker.txt` and run
`PYTHONPATH=src python3 -m dian_booker --local --dry-run --trigger found`. It uses your own
Chrome window and doesn't send a link.

Your personal data comes only from secrets/env vars. It is never logged, and screenshots go
only to Telegram.

## Event log and reports

The monitor and the booker append JSON lines to `state/events.jsonl` (on Render,
`/var/data/state/events.jsonl`), which persists with the state. A booker started from Render
writes its own log, uploaded as `booker-events-<run_id>`. They write:
- one summary per run: checks, failures, slowest answer, and how the run ended;
- every opening and closing, failure, block, and opening in another city;
- for each booker attempt: when each stage was reached, how long each DIAN wizard step took,
  how long the captcha waited, and the result.

On GitHub, every monitor run uploads the whole log as an `events-<run_id>` artifact. To read it:

```bash
scripts/report.sh 7     # newest log from GitHub, last 7 days (needs gh)
PYTHONPATH=src python3 -m dian_monitor --report path/to/events.jsonl
```

The log never contains personal data or the live link. Booker error texts are scrubbed of
your name, document, email and phone before they're written.

## Settings (environment variables)

`AIRTABLE_TOKEN` / `AIRTABLE_BASE_ID` / `AIRTABLE_TABLE`, `DIAN_CASES_REFRESH_HOURS`,
`DIAN_CASES_CACHE_FILE`, `DIAN_SERVICE` (default `vehiculos`), `DIAN_MAX_PARALLEL_BOOKERS`,
`DIAN_CITY` (without cases; default `medellin`), `DIAN_TIPO_PERSONA`, `DIAN_TIPO_ATENCION`, `DIAN_CATEGORIA`,
`DIAN_RUN_BUDGET_SECONDS`, `DIAN_REALERT_SECONDS`, `DIAN_MAX_REALERTS`,
`DIAN_FAILURES_BEFORE_ALERT`, `DIAN_HEARTBEAT_HOUR`, `DIAN_BOOKER`, `DIAN_BOOKER_REPO`,
`DIAN_HEALTHCHECK_URL`. See `src/dian_monitor/config.py`.
