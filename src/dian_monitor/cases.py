"""Booking cases: whom to book and in which city.

The cases come from the Airtable "Cases" table (airtable.py). The tramite is the same for every
case (settings.service, IVA vehículos), and the document type is always cédula de ciudadanía.

The monitor only needs the routing fields (id, city, status), so it never asks Airtable for the
personal ones. The booker loads its one case with the personal fields (`Case.personal`).

The case id appears in public logs, events and GitHub run names, so it must not carry personal
data: short, lowercase letters, digits and dashes, and no runs of 5+ digits (a document number).
Error messages name a case by its id only, never by a personal value.

Without Airtable there are no cases: nothing is watched and no booker starts.
"""
import os
import re
from dataclasses import dataclass, field

from .check import normalize

# DIAN requires an appointment to file a devolución by hand only at these seccionales
# (dian.gov.co, "Devoluciones y compensaciones"); everywhere else documents go by email.
ALLOWED_CITIES = {"bogota": "Bogotá", "bucaramanga": "Bucaramanga", "cali": "Cali",
                  "medellin": "Medellín"}
PENDING, PAUSED, BOOKED = "pending", "paused", "booked"
STATUSES = (PENDING, PAUSED, BOOKED)
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
        """How messages name the case: its city and id."""
        return f"{self.city_name} ({self.id})"

    def routing(self):
        """What may be stored or logged: no personal data."""
        return {"id": self.id, "city": self.city, "status": self.status,
                "record_id": self.record_id}


def check_id(cid, where):
    if not isinstance(cid, str) or not ID_RE.match(cid) or re.search(r"\d{5}", cid):
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


def booking_file(settings, case_id):
    """Where a booking for this case is recorded."""
    root, ext = os.path.splitext(settings.booking_file)
    return f"{root}-{case_id}{ext or '.json'}"


def booked_artifact(case_id):
    """GitHub artifact booker.yml uploads after a real booking for this case."""
    return f"booking-booked-{case_id}"
