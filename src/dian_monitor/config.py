"""Settings, all overridable through environment variables."""
import os
from dataclasses import dataclass, field


def _env(name, default):
    value = os.environ.get(name, "").strip()
    return value if value else default


def _flag(name, default):
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    # Codes used by the DIAN app itself:
    #   tipo_persona : 1 = Persona Natural, 2 = Persona Juridica
    #   tipo_atencion: 1 = Presencial,      2 = Virtual (Videoatencion)
    #   categoria    : 13 = Devoluciones (7 RUT, 11 Conferencias, 15 NAF,
    #                  16 Grandes Contribuyentes, 17 Cobranzas, 19 Defensoria)
    tipo_persona: str = "1"
    tipo_atencion: str = "2"
    categoria: str = "13"
    # Accent/case-insensitive city matched against the "<City> - ..." prefix of each tramite.
    # Used only without cases (one "default" case); with cases, each case has its city.
    city: str = "medellin"
    # The one tramite every case is for: only tramites whose name contains it are watched and
    # booked, with no fallback to another tramite of the city.
    service: str = "vehiculos"
    # The cases (people to book and their cities, see cases.py). Airtable when a token and base
    # are set; otherwise an inline JSON or a file (local runs and tests).
    airtable_token: str = field(default="", repr=False)  # secret: never logged
    airtable_base: str = ""
    airtable_table: str = "Cases"   # name or tbl… id
    # Bogotá hours after which the monitor reads the cases again (it also reads them at start).
    cases_refresh_hours: tuple = (6, 11, 14)
    # Read the cases at startup even if the cached copy is from after the last refresh hour.
    # The GitHub fallback monitor starts every ~20 min, so it turns this off to save calls.
    cases_refresh_on_start: bool = True
    # Last good read (routing only: id, city, status; no personal data), used if Airtable fails.
    cases_cache_file: str = "state/cases.json"
    cases_json: str = ""
    cases_file: str = ""

    state_file: str = "state/state.json"
    captures_dir: str = "captures"
    # Seconds one invocation keeps polling. The workflow is scheduled every 5 min
    # with a concurrency group, so a long budget bridges GitHub's cron delays.
    # With --forever (Render) it is the length of each run summary in the event log.
    run_budget_seconds: int = 1140
    # While a slot stays open, repeat the alert this often, at most this many times.
    realert_seconds: int = 300
    max_realerts: int = 3
    # Consecutive failed checks before alerting that the bot itself is broken.
    failures_before_alert: int = 3
    # Bogota hour after which the first run of the day sends the "still alive" summary.
    heartbeat_hour: int = 8
    booking_url: str = "https://agendamiento.dian.gov.co/?recurso=CitasDIAN"
    # Start `python -m dian_booker` on an opening. The booker stops at the reCAPTCHA and sends a
    # live-browser link for a human to solve it.
    booker: bool = False
    # Also start it, silently, for each case whose city is not open when a new tramite opens
    # anywhere: DIAN releases cities in waves, so it watches the case's city for a few
    # minutes. It never books or alerts for another city.
    booker_on_other_city: bool = True
    # "owner/repo": start the booker as a GitHub Actions run (booker.yml) instead of a local
    # process. Used when the monitor runs on Render; needs GITHUB_DISPATCH_TOKEN.
    booker_repo: str = ""
    # At most this many booker runs at once (one per case), so a wave of openings can't start
    # a browser per case all polling DIAN. A local booker process always runs one at a time.
    max_parallel_bookers: int = 4
    # Written by the booker after a confirmed booking; while it says booked, no more bookers.
    booking_file: str = "state/booking.json"
    # Structured event log (JSONL) for reports; kept with the state, uploaded after each run.
    events_file: str = "state/events.jsonl"
    # Dead-man's switch (e.g. https://hc-ping.com/<uuid>): pinged while the monitor is alive.
    # It contains a secret key, so it is never logged.
    healthcheck_url: str = ""

    @classmethod
    def from_env(cls):
        return cls(
            tipo_persona=_env("DIAN_TIPO_PERSONA", cls.tipo_persona),
            tipo_atencion=_env("DIAN_TIPO_ATENCION", cls.tipo_atencion),
            categoria=_env("DIAN_CATEGORIA", cls.categoria),
            city=_env("DIAN_CITY", cls.city),
            service=_env("DIAN_SERVICE", cls.service),
            airtable_token=_env("AIRTABLE_TOKEN", ""),
            airtable_base=_env("AIRTABLE_BASE_ID", cls.airtable_base),
            airtable_table=_env("AIRTABLE_TABLE", cls.airtable_table),
            cases_refresh_hours=tuple(int(h) for h in _env(
                "DIAN_CASES_REFRESH_HOURS", ",".join(map(str, cls.cases_refresh_hours))).split(",")
                if h.strip()),
            cases_refresh_on_start=_flag("DIAN_CASES_REFRESH_ON_START", cls.cases_refresh_on_start),
            cases_cache_file=_env("DIAN_CASES_CACHE_FILE", cls.cases_cache_file),
            cases_json=_env("DIAN_CASES_JSON", cls.cases_json),
            cases_file=_env("DIAN_CASES_FILE", cls.cases_file),
            state_file=_env("DIAN_STATE_FILE", cls.state_file),
            captures_dir=_env("DIAN_CAPTURES_DIR", cls.captures_dir),
            run_budget_seconds=int(_env("DIAN_RUN_BUDGET_SECONDS", cls.run_budget_seconds)),
            realert_seconds=int(_env("DIAN_REALERT_SECONDS", cls.realert_seconds)),
            max_realerts=int(_env("DIAN_MAX_REALERTS", cls.max_realerts)),
            failures_before_alert=int(_env("DIAN_FAILURES_BEFORE_ALERT", cls.failures_before_alert)),
            heartbeat_hour=int(_env("DIAN_HEARTBEAT_HOUR", cls.heartbeat_hour)),
            booker=_flag("DIAN_BOOKER", cls.booker),
            booker_on_other_city=_flag("DIAN_BOOKER_ON_OTHER_CITY", cls.booker_on_other_city),
            booker_repo=_env("DIAN_BOOKER_REPO", cls.booker_repo),
            max_parallel_bookers=int(_env("DIAN_MAX_PARALLEL_BOOKERS", cls.max_parallel_bookers)),
            booking_file=_env("DIAN_BOOKING_FILE", cls.booking_file),
            events_file=_env("DIAN_EVENTS_FILE", cls.events_file),
            healthcheck_url=_env("DIAN_HEALTHCHECK_URL", cls.healthcheck_url),
        )

    @property
    def label(self):
        persona = "Persona Natural" if self.tipo_persona == "1" else "Persona Juridica"
        atencion = "Videoatención" if self.tipo_atencion == "2" else "Presencial"
        categoria = CATEGORIAS.get(self.categoria, self.categoria)
        return f"{persona} / {atencion} / {categoria}"


CATEGORIAS = {
    "7": "RUT y orientación TAC", "11": "Conferencias o capacitaciones",
    "13": "Devoluciones", "15": "Autogestión servicios en línea con NAF",
    "16": "Inconsistencias Grandes Contribuyentes", "17": "Cobranzas",
    "19": "Defensoría",
}
