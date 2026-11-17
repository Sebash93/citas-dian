"""The cases from the Airtable "Cases" table, and when to read them.

Table fields (see the table's own field descriptions):
    ID (autonumber, the case is "case-<ID>"), Full name, City, Document number, Email, Phone,
    Status (Pending / Paused / Booked; empty = Pending), Appointment, Booked at.

The free plan allows 1,000 API calls a month, so:
- the monitor reads only the routing fields (ID, City, Status), never the personal ones, at
  startup and then once after each refresh hour (Bogotá, default 6, 11 and 14): `CasesFeed`;
- a booker reads its own record (personal fields) only when it is about to need them, and
  writes the booking back once (`Airtable.case`, `Airtable.mark_booked`).
Every call is emitted as an `airtable_call` event so the report can count them.

The token is a secret: errors carry only the HTTP status and Airtable's error type/message.
"""
import json
import os
from datetime import datetime, timedelta

import requests

from .cases import (BOOKED, PENDING, Case, CasesError, check_city, check_status)

API = "https://api.airtable.com/v0/{base}/{table}"
F_ID, F_NAME, F_CITY, F_DOC = "ID", "Full name", "City", "Document number"
F_EMAIL, F_PHONE, F_STATUS = "Email", "Phone", "Status"
F_APPOINTMENT, F_BOOKED_AT = "Appointment", "Booked at"
ROUTING = (F_ID, F_CITY, F_STATUS)
PERSONAL = {F_NAME: "full_name", F_DOC: "document_number", F_EMAIL: "email", F_PHONE: "phone"}
STATUS_NAMES = {PENDING: "Pending", BOOKED: "Booked"}


class AirtableError(Exception):
    pass


def case_id(number):
    return f"case-{int(number)}"


def number_of(cid):
    """'case-12' -> 12, or None."""
    head, _, num = str(cid).partition("-")
    return int(num) if head == "case" and num.isdigit() else None


class Airtable:
    def __init__(self, token, base, table="Cases", session=None, on_call=None):
        self.token = token
        self.base = base
        self.table = table
        self.s = session or requests.Session()
        self.on_call = on_call or (lambda op, ok: None)

    @classmethod
    def from_settings(cls, settings, on_call=None):
        if not (settings.airtable_token and settings.airtable_base):
            return None
        return cls(settings.airtable_token, settings.airtable_base, settings.airtable_table,
                   on_call=on_call)

    def _call(self, op, method, path="", **kwargs):
        url = API.format(base=self.base, table=requests.utils.quote(self.table, safe="")) + path
        try:
            resp = self.s.request(method, url, timeout=20,
                                  headers={"Authorization": f"Bearer {self.token}"}, **kwargs)
        except requests.RequestException as e:
            self.on_call(op, False)
            raise AirtableError(f"Airtable {op}: {type(e).__name__}") from e
        ok = resp.status_code < 300
        self.on_call(op, ok)
        if not ok:
            try:
                err = resp.json().get("error")
                detail = (f"{err.get('type', '')}: {err.get('message', '')}"
                          if isinstance(err, dict) else str(err or ""))
            except ValueError:
                detail = ""
            raise AirtableError(f"Airtable {op}: HTTP {resp.status_code} {detail}".strip())
        return resp.json()

    def routing_cases(self):
        """Every case's id, city and status (never personal data), and the records skipped
        as invalid: ([Case], ["case-7: city must be one of ..."])."""
        cases, skipped, offset = [], [], None
        while True:
            params = {"fields[]": list(ROUTING), "pageSize": 100}
            if offset:
                params["offset"] = offset
            body = self._call("list", "GET", params=params)
            for rec in body.get("records", []):
                try:
                    cases.append(self._case(rec, personal=False))
                except CasesError as e:
                    skipped.append(str(e))
            offset = body.get("offset")
            if not offset:
                return cases, skipped

    def case(self, cid):
        """One case with its personal fields. Raises CasesError if it is not in the table."""
        number = number_of(cid)
        if number is None:
            raise CasesError(f"case {cid!r} is not an Airtable case id (case-<ID>)")
        body = self._call("get", "GET", params={
            "filterByFormula": f"{{{F_ID}}}={number}", "maxRecords": 1,
            "fields[]": list(ROUTING) + list(PERSONAL)})
        records = body.get("records") or []
        if not records:
            raise CasesError(f"case {cid!r} is not in the Airtable table")
        return self._case(records[0], personal=True)

    def mark_booked(self, record_id, appointment, booked_at):
        self._call("update", "PATCH", f"/{record_id}", json={"fields": {
            F_STATUS: STATUS_NAMES[BOOKED], F_APPOINTMENT: appointment,
            F_BOOKED_AT: booked_at}})

    @staticmethod
    def _case(rec, personal):
        fields = rec.get("fields") or {}
        if not isinstance(fields.get(F_ID), (int, float)):
            raise CasesError("a record has no ID (the ID field must be an autonumber)")
        cid = case_id(fields[F_ID])
        return Case(id=cid, city=check_city(fields.get(F_CITY), cid),
                    status=check_status(fields.get(F_STATUS), cid),
                    record_id=str(rec.get("id") or ""),
                    personal={key: str(fields[name]) for name, key in PERSONAL.items()
                              if fields.get(name)} if personal else {})


# --- when the monitor reads the cases ------------------------------------------------------

RETRY_MINUTES = (10, 20, 40, 60)  # after a failed read; the cache is used meanwhile


def last_slot(now, hours):
    """The latest time <= now at one of `hours` (Bogotá, on the hour)."""
    for back in range(2):
        day = (now - timedelta(days=back)).replace(minute=0, second=0, microsecond=0)
        slots = [day.replace(hour=h) for h in sorted(hours, reverse=True)]
        for slot in slots:
            if slot <= now:
                return slot
    return None


class CasesFeed:
    """Keeps the monitor's cases (routing only) in memory, read from Airtable at startup and
    after each refresh hour. Each good read is also saved to `cache_file` (no personal data),
    which is used when Airtable can't be read, so a restart during an Airtable outage still
    watches the last known cases."""

    def __init__(self, airtable, hours, cache_file, clock, log, refresh_on_start=True):
        self.airtable = airtable
        self.hours = hours
        self.cache_file = cache_file
        self.clock = clock
        self.log = log
        self.refresh_on_start = refresh_on_start
        self.cases = None
        self.fetched_at = None   # last good read from Airtable
        self.retry_at = None
        self.failures = 0
        self.skipped = []

    def due(self, now):
        if self.retry_at is not None:
            return now >= self.retry_at
        slot = last_slot(now, self.hours)
        return self.fetched_at is None or (slot is not None and self.fetched_at < slot)

    def start(self):
        """Startup read. Returns (cases, problem or None)."""
        cached, cached_at = self._read_cache()
        if not self.refresh_on_start and cached is not None:
            self.cases, self.fetched_at = cached, cached_at
            slot = last_slot(self.clock(), self.hours)
            if cached_at is not None and (slot is None or cached_at >= slot):
                return self.cases, None   # fresh enough: no call
        return self.refresh()

    def refresh(self):
        """Read now. Returns (cases, problem or None); on failure keeps the last known cases."""
        now = self.clock()
        try:
            cases, self.skipped = self.airtable.routing_cases()
        except AirtableError as e:
            self.failures += 1
            self.retry_at = now + timedelta(
                minutes=RETRY_MINUTES[min(self.failures, len(RETRY_MINUTES)) - 1])
            if self.cases is None:
                self.cases, self.fetched_at = self._read_cache()
                self.fetched_at = None  # read again as soon as Airtable answers
            self.log(f"Cases: {e}; retrying at {self.retry_at:%H:%M}"
                     f"{'' if self.cases is None else ' (using the last known cases)'}.")
            return self.cases or [], str(e)
        self.failures, self.retry_at = 0, None
        self.cases, self.fetched_at = cases, now
        self._write_cache(cases, now)
        return cases, None

    def _read_cache(self):
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                data = json.load(f)
            cases = [Case(id=c["id"], city=c["city"], status=c["status"],
                          record_id=c.get("record_id", "")) for c in data["cases"]]
            at = data.get("fetched_at")
            return cases, datetime.fromisoformat(at) if at else None
        except (OSError, ValueError, KeyError, TypeError):
            return None, None

    def _write_cache(self, cases, now):
        try:
            os.makedirs(os.path.dirname(self.cache_file) or ".", exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now.isoformat(timespec="seconds"),
                           "cases": [c.routing() for c in cases]}, f, indent=2)
            os.replace(tmp, self.cache_file)
        except OSError as e:
            self.log(f"Cases: could not save the cache ({type(e).__name__}).")

