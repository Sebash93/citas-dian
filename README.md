# citas-dian

Watches [agendamiento.dian.gov.co](https://agendamiento.dian.gov.co/?recurso=CitasDIAN) for
**Persona Natural → Videoatención → Devoluciones** slots in **Medellín** and alerts as soon
as one opens.

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
| Fri 8:30–12:30, 14:00–17:00 | ~45 s |
| Tue/Wed 8:00–12:00 | ~50 s |
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

## GitHub Actions

- `monitor.yml` starts every 5 minutes; each run polls for about 19 minutes and a
  concurrency group queues the next one, so coverage stays continuous even when cron is late.
  State (deduplication, failures, history) is kept in the Actions cache, and raw responses
  for any opening are uploaded as `captures-*` artifacts.
- `keepalive.yml` re-enables the schedule twice a month (GitHub disables it after 60 days
  without repository activity).
- Run manually from **Actions → Monitor citas DIAN → Run workflow** for a single check.

The repo must be public for unlimited free Actions minutes. Logs contain no personal data.

## Booker (optional)

When Medellín opens (or DIAN gets slow, a "posible" alert), the monitor starts
`python -m dian_booker` on the same runner:

1. It sets up a phone-sized virtual screen (Xvfb → x11vnc → noVNC) behind a Cloudflare quick
   tunnel and opens Chrome on the screen.
2. It sends **"PREPÁRATE"** with the link right away, so you can open it while the bot works.
3. It goes through Persona Natural → Videoatención → Devoluciones → Medellín, then picks the
   earliest date and time and your document type. Each step takes 5–10 s when slots are open.
4. It sends **"TOCA EL CAPTCHA"**. You tap "No soy un robot" in the live view.
5. It types your document number, fills in or confirms your details, accepts the data policy,
   clicks Agendar, and sends you the confirmation and a screenshot.

If anything fails after the link was sent, the browser stays open 10 minutes so you can finish
by hand. Add more people to `TELEGRAM_CHAT_ID` (comma-separated) so that anyone you trust can
tap the captcha.

Setup:
- Repo variable `DIAN_BOOKER=1`. Set `BOOKER_DRY_RUN=1` to stop before Agendar while testing.
- Secrets: `BOOKER_DOC_NUMBER`, `BOOKER_FULL_NAME`, `BOOKER_EMAIL`, `BOOKER_PHONE`, and
  optionally `BOOKER_DOC_TYPE` (default "Cédula de ciudadanía").
- To test: **Actions → Booker test run** walks any open category (default: Presencial / RUT /
  Medellín) as a dry run and sends you the link.

Locally: `pip install -r requirements-booker.txt` and run
`PYTHONPATH=src python3 -m dian_booker --local --dry-run --trigger found`. It uses your own
Chrome window and doesn't send a link.

Your personal data comes only from secrets/env vars. It is never logged, and screenshots go
only to Telegram.

## Event log and reports

The monitor and the booker append JSON lines to `state/events.jsonl`, which persists with
the state. They write:
- one summary per run: checks, failures, slowest answer, and how the run ended;
- every opening and closing, failure, block, slow DIAN answer, and opening in another city;
- for each booker attempt: when each stage was reached, how long each DIAN wizard step took,
  how long the captcha waited, and the result.

Every run uploads the whole log as an `events-<run_id>` artifact. To read it:

```bash
scripts/report.sh 7     # newest log from GitHub, last 7 days (needs gh)
PYTHONPATH=src python3 -m dian_monitor --report path/to/events.jsonl
```

The log never contains personal data or the live link. Booker error texts are scrubbed of
your name, document, email and phone before they're written.

## Settings (environment variables)

`DIAN_CITY` (default `medellin`), `DIAN_TIPO_PERSONA`, `DIAN_TIPO_ATENCION`, `DIAN_CATEGORIA`,
`DIAN_RUN_BUDGET_SECONDS`, `DIAN_REALERT_SECONDS`, `DIAN_MAX_REALERTS`,
`DIAN_FAILURES_BEFORE_ALERT`, `DIAN_HEARTBEAT_HOUR`. See `src/dian_monitor/config.py`.
