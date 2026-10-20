"""Settings, all overridable through environment variables."""
import os
from dataclasses import dataclass


def _env(name, default):
    value = os.environ.get(name, "").strip()
    return value if value else default


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
    # Accent/case-insensitive substring matched against each available tramite.
    city: str = "medellin"

    state_file: str = "state/state.json"
    captures_dir: str = "captures"
    # Seconds one invocation keeps polling. The workflow is scheduled every 5 min
    # with a concurrency group, so a long budget bridges GitHub's cron delays.
    run_budget_seconds: int = 1140
    # While a slot stays open, repeat the alert this often, at most this many times.
    realert_seconds: int = 300
    max_realerts: int = 3
    # Consecutive failed checks before alerting that the bot itself is broken.
    failures_before_alert: int = 3
    # Bogota hour after which the first run of the day sends the "still alive" summary.
    heartbeat_hour: int = 8
    booking_url: str = "https://agendamiento.dian.gov.co/?recurso=CitasDIAN"

    @classmethod
    def from_env(cls):
        return cls(
            tipo_persona=_env("DIAN_TIPO_PERSONA", cls.tipo_persona),
            tipo_atencion=_env("DIAN_TIPO_ATENCION", cls.tipo_atencion),
            categoria=_env("DIAN_CATEGORIA", cls.categoria),
            city=_env("DIAN_CITY", cls.city),
            state_file=_env("DIAN_STATE_FILE", cls.state_file),
            captures_dir=_env("DIAN_CAPTURES_DIR", cls.captures_dir),
            run_budget_seconds=int(_env("DIAN_RUN_BUDGET_SECONDS", cls.run_budget_seconds)),
            realert_seconds=int(_env("DIAN_REALERT_SECONDS", cls.realert_seconds)),
            max_realerts=int(_env("DIAN_MAX_REALERTS", cls.max_realerts)),
            failures_before_alert=int(_env("DIAN_FAILURES_BEFORE_ALERT", cls.failures_before_alert)),
            heartbeat_hour=int(_env("DIAN_HEARTBEAT_HOUR", cls.heartbeat_hour)),
        )

    @property
    def label(self):
        persona = "Persona Natural" if self.tipo_persona == "1" else "Persona Juridica"
        atencion = "Videoatención" if self.tipo_atencion == "2" else "Presencial"
        categoria = CATEGORIAS.get(self.categoria, self.categoria)
        return f"{persona} / {atencion} / {categoria} / {self.city.title()}"


CATEGORIAS = {
    "7": "RUT y orientación TAC", "11": "Conferencias o capacitaciones",
    "13": "Devoluciones", "15": "Autogestión servicios en línea con NAF",
    "16": "Inconsistencias Grandes Contribuyentes", "17": "Cobranzas",
    "19": "Defensoría",
}
