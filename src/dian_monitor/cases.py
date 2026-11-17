"""Booking cases: whom to book and in which city.

The cases come from the Airtable "Cases" table (airtable.py), or for local runs and tests from a
cases JSON (`DIAN_CASES_JSON`, or a file in `DIAN_CASES_FILE`) with the same keys:

    {"cases": [{"id": "case-1", "city": "medellin", "status": "pending",
                "full_name": "...", "document_number": "...", "email": "...", "phone": "..."}]}

The tramite is the same for every case (settings.service, IVA vehículos), and the document type
is always cédula de ciudadanía.

The monitor only needs the routing fields (id, city, status), so it never keeps the personal
ones: `parse(..., personal=False)` drops them, and from Airtable it never asks for them. The
booker loads its one case with the personal fields.

The case id appears in public logs, events and GitHub run names, so it must not carry personal
data: short, lowercase letters, digits and dashes, and no runs of 5+ digits (a document number).
Error messages name a case by its position and id only, never by a personal value.

Without any cases source there is one "default" case for DIAN_CITY, with the person from the
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
PENDING, PAUSED, BOOKED = "pending", "paused", "booked"
STATUSES = (PENDING, PAUSED, BOOKED)
ROUTING_FIELDS = ("id", "city", "status")
PERSONAL_FIELDS = ("full_name", "document_number", "email", "phone")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


class CasesError(ValueError):
    pass


def city_name(city):
    return ALLOWED_CITIES.get(normalize(city), str(city).title())


@dataclass(frozen=True)
class Case:
    id: str
    city: str                 # normalized, e.g. "medellin"
    status: str = PENDING     # pending: look for an appointment; paused: ignore; booked: done
    record_id: str = ""       # Airtable record id (not personal), to write the booking back
    personal: dict = field(default_factory=dict, repr=False, compare=False, hash=False)

    @property
    def enabled(self):
        return self.status == PENDING

    @property
    def city_name(self):
        return city_name(self.city)

    @property
    def who(self):
        """How messages name the case: the city, plus the id when there are cases."""
        return self.city_name if self.id == DEFAULT_ID else f"{self.city_name} ({self.id})"

    def routing(self):
        """What may be stored or logged: no personal data."""
        return {"id": self.id, "city": self.city, "status": self.status,
                "record_id": self.record_id}


def check_id(cid, where):
    if not isinstance(cid, str) or not ID_RE.match(cid) or re.search(r"\d{5}", cid) \
            or cid == DEFAULT_ID:
        raise CasesError(f"{where}: id must be 1-24 lowercase letters, digits or dashes, "
                         "with no personal data (no long numbers)")
    return cid


def check_city(city, where):
    city = normalize(city or "")
    if city not in ALLOWED_CITIES:
        raise CasesError(f"{where}: city must be one of {sorted(ALLOWED_CITIES)}")
    return city


def check_status(status, where):
    status = normalize(status or PENDING)
    if status not in STATUSES:
        raise CasesError(f"{where}: status must be one of {list(STATUSES)}")
    return status


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
        cid = check_id(item.get("id"), f"case #{n}")
        if cid in seen:
            raise CasesError(f"case #{n}: duplicate id {cid!r}")
        seen.add(cid)
        unknown = sorted(set(item) - set(ROUTING_FIELDS) - set(PERSONAL_FIELDS) - {"record_id"})
        if unknown:
            raise CasesError(f"case {cid!r}: unknown fields {unknown}")
        cases.append(Case(
            id=cid, city=check_city(item.get("city"), f"case {cid!r}"),
            status=check_status(item.get("status"), f"case {cid!r}"),
            record_id=str(item.get("record_id") or ""),
            personal={k: str(item[k]) for k in PERSONAL_FIELDS if item.get(k)} if personal else {}))
    return cases


def from_json_settings(settings, personal=False):
    """Cases from DIAN_CASES_JSON / DIAN_CASES_FILE, or None when neither is set."""
    if settings.cases_json:
        return parse(settings.cases_json, personal)
    if settings.cases_file:
        try:
            with open(settings.cases_file, encoding="utf-8") as f:
                return parse(f.read(), personal)
        except OSError as e:
            raise CasesError(f"cannot read DIAN_CASES_FILE ({type(e).__name__})") from None
    return None


def default_case(settings):
    return Case(DEFAULT_ID, normalize(settings.city))


def booking_file(settings, case_id):
    """Where a booking for this case is recorded (the default case keeps the old path)."""
    if case_id == DEFAULT_ID:
        return settings.booking_file
    root, ext = os.path.splitext(settings.booking_file)
    return f"{root}-{case_id}{ext or '.json'}"


def booked_artifact(case_id):
    """GitHub artifact booker.yml uploads after a real booking for this case."""
    return "booking-booked" if case_id == DEFAULT_ID else f"booking-booked-{case_id}"
