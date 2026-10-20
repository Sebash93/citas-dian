# citas-dian

Watches [agendamiento.dian.gov.co](https://agendamiento.dian.gov.co/?recurso=CitasDIAN) for
**Persona Natural → Videoatención → Devoluciones** slots in **Medellín** and alerts as soon
as one opens.

**Notify only.** It never books, never sends personal data, and never tries to get past the
reCAPTCHA that guards the booking step. You book by hand from the alert.

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

## Settings (environment variables)

`DIAN_CITY` (default `medellin`), `DIAN_TIPO_PERSONA`, `DIAN_TIPO_ATENCION`, `DIAN_CATEGORIA`,
`DIAN_RUN_BUDGET_SECONDS`, `DIAN_REALERT_SECONDS`, `DIAN_MAX_REALERTS`,
`DIAN_FAILURES_BEFORE_ALERT`, `DIAN_HEARTBEAT_HOUR`. See `src/dian_monitor/config.py`.
