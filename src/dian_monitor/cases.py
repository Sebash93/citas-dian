"""Booking cases: whom to book and in which city.

A cases JSON (`DIAN_CASES_JSON`, or a file in `DIAN_CASES_FILE`) lists one entry per person:

    {"cases": [{"id": "med-1", "city": "medellin", "service": "vehiculos", "enabled": true,
                "doc_type": "Cédula de ciudadanía", "doc_number": "...", "full_name": "...",
                "email": "...", "phone": "...", "telegram_chat": ""}]}

The monitor alerts for every city of an enabled case and starts one booker per case. It only
needs the routing fields (id, city, service, enabled), so it never keeps the personal ones:
`load(..., personal=False)` drops them. The booker loads its one case with `personal=True`.

The case id appears in public logs, events and GitHub run names, so it must not carry personal
data: short, lowercase letters, digits and dashes, and no runs of 5+ digits (a document number).
Error messages name a case by its position and id only, never by a personal value.

Without a cases JSON there is one "default" case for DIAN_CITY, with the person from the
BOOKER_* environment variables (how the project worked before cases).
"""
import json
import os
import re
from dataclasses import dataclass, field

from .check import normalize

# DIAN requires an appointment to file a devolución by hand only at these seccionales
# (dian.gov.co, "Devoluciones y compensaciones"); everywhere else documents go by email.
ALLOWED_CITIES = {"bogota": "Bogotá", "bucaramanga": "Bucaramanga", "cali": "Cali",
                  "medellin": "Medellín"}
DEFAULT_ID = "default"
ROUTING_FIELDS = ("id", "city", "service", "enabled", "telegram_chat")
PERSONAL_FIELDS = ("doc_type", "doc_number", "full_name", "email", "phone")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


class CasesError(ValueError):
    pass


def city_name(city):
    return ALLOWED_CITIES.get(normalize(city), str(city).title())


@dataclass(frozen=True)
class Case:
    id: str
    city: str                 # normalized, e.g. "medellin"
    service: str = ""         # preferred tramite name parts, comma-separated (e.g. "vehiculos")
    enabled: bool = True
    telegram_chat: str = ""   # where the booker sends this case's links; empty = TELEGRAM_CHAT_ID
    personal: dict = field(default_factory=dict, repr=False, compare=False, hash=False)

    @property
    def city_name(self):
        return city_name(self.city)

    @property
    def who(self):
        """How messages name the case: the city, plus the id when there are cases."""
        return self.city_name if self.id == DEFAULT_ID else f"{self.city_name} (caso {self.id})"


def parse(text, personal=False):
    try:
        data = json.loads(text)
    except ValueError:
        raise CasesError("the cases JSON is not valid JSON") from None  # never echo the text
    items = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise CasesError('the cases JSON needs a non-empty "cases" list')
    cases, seen = [], set()
    for n, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise CasesError(f"case #{n} is not an object")
        cid = item.get("id")
        if not isinstance(cid, str) or not ID_RE.match(cid) or re.search(r"\d{5}", cid) \
                or cid == DEFAULT_ID:
            raise CasesError(f"case #{n}: id must be 1-24 lowercase letters, digits or dashes, "
                             "with no personal data (no long numbers)")
        if cid in seen:
            raise CasesError(f"case #{n}: duplicate id {cid!r}")
        seen.add(cid)
        unknown = sorted(set(item) - set(ROUTING_FIELDS) - set(PERSONAL_FIELDS))
        if unknown:
            raise CasesError(f"case {cid!r}: unknown fields {unknown}")
        city = normalize(item.get("city") or "")
        if city not in ALLOWED_CITIES:
            raise CasesError(f"case {cid!r}: city must be one of {sorted(ALLOWED_CITIES)}")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise CasesError(f"case {cid!r}: enabled must be true or false")
        cases.append(Case(
            id=cid, city=city, service=str(item.get("service") or "").strip(), enabled=enabled,
            telegram_chat=str(item.get("telegram_chat") or "").strip(),
            personal={k: str(item[k]) for k in PERSONAL_FIELDS if item.get(k)} if personal else {}))
    return cases


def configured(settings):
    return bool(settings.cases_json or settings.cases_file)


def load(settings, personal=False):
    """All cases from the settings, or the one "default" case when none are configured."""
    if settings.cases_json:
        return parse(settings.cases_json, personal)
    if settings.cases_file:
        try:
            with open(settings.cases_file, encoding="utf-8") as f:
                return parse(f.read(), personal)
        except OSError as e:
            raise CasesError(f"cannot read DIAN_CASES_FILE ({type(e).__name__})") from None
    return [Case(DEFAULT_ID, normalize(settings.city))]


def find(settings, case_id):
    """The one case to book, with its personal data. Raises CasesError."""
    if not configured(settings):
        if case_id != DEFAULT_ID:
            raise CasesError(f"case {case_id!r} given but no cases JSON is configured")
        return load(settings)[0]
    case = next((c for c in load(settings, personal=True) if c.id == case_id), None)
    if case is None:
        raise CasesError(f"case {case_id!r} is not in the cases JSON")
    if not case.enabled:
        raise CasesError(f"case {case_id!r} is disabled")
    return case


def booking_file(settings, case_id):
    """Where a booking for this case is recorded (the default case keeps the old path)."""
    if case_id == DEFAULT_ID:
        return settings.booking_file
    root, ext = os.path.splitext(settings.booking_file)
    return f"{root}-{case_id}{ext or '.json'}"


def booked_artifact(case_id):
    """GitHub artifact booker.yml uploads after a real booking for this case."""
    return "booking-booked" if case_id == DEFAULT_ID else f"booking-booked-{case_id}"
